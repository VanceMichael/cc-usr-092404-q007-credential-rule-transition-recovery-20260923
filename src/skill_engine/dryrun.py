"""换版影响演练。

保证点：
- input_summary 覆盖水位、草案内容与全部基础数据：相同草案在相同水位重跑直接复用
  已完成演练；局部修改生成新演练，但按项目内容寻址缓存只重算受影响项目。
- 每个批次独立提交并推进断点，长批次中断后从断点继续。
- 变更决定与撤销风险在同事务写入持久待发箱；幂等键绑定裁定输入切片，
  重启重跑既不漏发也不会让同一收件人收到重复决定。
"""

import json

from sqlalchemy import select

from .errors import InvalidState, NotFound
from .evaluation import (
    Watermark,
    evaluate_project,
    load_version,
    project_input_key,
    resolve_rule,
)
from .models import (
    decision_cache,
    dry_run_checkpoints,
    dry_run_results,
    dry_runs,
    outbox,
    positions,
    projects,
)
from .util import canonical, digest, new_id, now_iso

DEFAULT_RECIPIENT = "compliance-lead"


def _base_data_fingerprint(conn) -> str:
    project_rows = [
        dict(r._mapping)
        for r in conn.execute(select(projects).order_by(projects.c.id)).fetchall()
    ]
    position_rows = [
        dict(r._mapping)
        for r in conn.execute(select(positions).order_by(positions.c.id)).fetchall()
    ]
    return digest({"projects": project_rows, "positions": position_rows})


def _run_summary(conn, *, candidate: dict, as_of: str, target_ids: list[str], mark: Watermark) -> str:
    return digest(
        {
            "as_of": as_of,
            "candidate": {
                "id": candidate["id"],
                "content_hash": candidate["content_hash"],
                "effective_from": candidate["effective_from"],
                "effective_to": candidate["effective_to"],
                "transition": candidate["transition_obj"],
                "supersedes_version_id": candidate["supersedes_version_id"],
            },
            "watermark": mark.global_fingerprint(),
            "base_data": _base_data_fingerprint(conn),
            "targets": target_ids,
        }
    )


def _target_projects(conn, candidate: dict, explicit_ids: list[str] | None) -> list[str]:
    if explicit_ids is not None:
        ids = sorted(explicit_ids)
        found = {
            r[0]
            for r in conn.execute(select(projects.c.id).where(projects.c.id.in_(ids))).fetchall()
        }
        missing = set(ids) - found
        if missing:
            raise NotFound(f"项目不存在: {sorted(missing)}")
        return ids
    rows = conn.execute(
        select(projects.c.id)
        .where(projects.c.region == candidate["region"])
        .where(projects.c.project_type == candidate["project_type"])
        .order_by(projects.c.id)
    ).fetchall()
    # 签署项目也纳入（展示固定快照结论），但范围外项目不纳入默认批次。
    return [r[0] for r in rows]


def _enqueue_notifications(conn, *, result: dict, project: dict, input_key: str) -> None:
    recipient = project.get("owner_contact") or DEFAULT_RECIPIENT
    backfill = sum(1 for p in result["positions"] if p["status"] == "backfill")
    review = sum(1 for p in result["positions"] if p["status"] == "review")
    exempt = sum(1 for p in result["positions"] if p["status"] == "exempt")
    revocation = sum(1 for p in result["positions"] if p["revocation_risks"])

    decision_key = f"decision:{input_key}"
    exists = conn.execute(
        select(outbox.c.id).where(outbox.c.idempotency_key == decision_key)
    ).fetchone()
    if exists is None and result["outcome"] != "compliant":
        conn.execute(
            outbox.insert().values(
                idempotency_key=decision_key,
                recipient=recipient,
                event_type="skill.impact.decision",
                subject=f"项目 {project['id']} 换版影响：{result['outcome']}",
                payload=canonical(
                    {
                        "project_id": project["id"],
                        "outcome": result["outcome"],
                        "rule_version_id": result["rule_version_id"],
                        "rule_selection_reason": result["rule_selection_reason"],
                        "counts": {"backfill": backfill, "review": review, "exempt": exempt},
                    }
                ),
                created_at=now_iso(),
            )
        )

    risk_key = f"revocation:{input_key}"
    exists = conn.execute(
        select(outbox.c.id).where(outbox.c.idempotency_key == risk_key)
    ).fetchone()
    if exists is None and revocation:
        risks = [
            {"position_id": p["position_id"], "risks": p["revocation_risks"]}
            for p in result["positions"]
            if p["revocation_risks"]
        ]
        conn.execute(
            outbox.insert().values(
                idempotency_key=risk_key,
                recipient=recipient,
                event_type="skill.impact.revocation_risk",
                subject=f"项目 {project['id']} 存在凭证撤销风险",
                payload=canonical({"project_id": project["id"], "revocations": risks}),
                created_at=now_iso(),
            )
        )


def _process_batch(conn, dry_run_id: str, target_ids: list[str], candidate_id: str,
                   as_of: str, mark: Watermark, batch_size: int) -> int:
    """处理一个批次，返回本批实际处理数量。结果与通知同事务提交。"""
    checkpoint = conn.execute(
        select(dry_run_checkpoints).where(dry_run_checkpoints.c.dry_run_id == dry_run_id)
    ).fetchone()
    start_index = checkpoint.last_order if checkpoint else 0
    remaining = target_ids[start_index:]
    batch = remaining if batch_size <= 0 else remaining[:batch_size]

    processed_hits = 0
    for offset, project_id in enumerate(batch):
        project_row = conn.execute(
            select(projects).where(projects.c.id == project_id)
        ).fetchone()
        project = dict(project_row._mapping)
        project["attributes_obj"] = json.loads(project["attributes"])

        candidate = load_version(conn, candidate_id)
        rule, rule_reason, rule_kind = resolve_rule(conn, project, candidate, as_of)
        pseudo = {"rule_selection_kind": rule_kind, "rule_version_id": rule["id"] if rule else None}
        input_key = project_input_key(
            conn, project_id=project_id, candidate_version_id=candidate_id,
            as_of=as_of, result=pseudo, watermark=mark,
        )

        cached = conn.execute(
            select(decision_cache).where(decision_cache.c.input_key == input_key)
        ).fetchone()
        if cached is not None:
            result = json.loads(cached.payload)
            from_cache = 1
            processed_hits += 1
        else:
            result = evaluate_project(
                conn, project_id=project_id, candidate_version_id=candidate_id,
                as_of=as_of, watermark=mark,
            )
            from_cache = 0
            conn.execute(
                decision_cache.insert().values(
                    input_key=input_key,
                    payload=canonical(result),
                    created_at=now_iso(),
                )
            )
            _enqueue_notifications(conn, result=result, project=project, input_key=input_key)

        conn.execute(
            dry_run_results.delete().where(dry_run_results.c.dry_run_id == dry_run_id)
            .where(dry_run_results.c.project_id == project_id)
        )
        conn.execute(
            dry_run_results.insert().values(
                id=new_id("res"),
                dry_run_id=dry_run_id,
                project_id=project_id,
                input_key=input_key,
                outcome=result["outcome"],
                rule_version_id=result["rule_version_id"],
                rule_selection_reason=result["rule_selection_reason"],
                detail=canonical(result),
                from_cache=from_cache,
            )
        )

    new_index = start_index + len(batch)
    if checkpoint is None:
        conn.execute(
            dry_run_checkpoints.insert().values(dry_run_id=dry_run_id, last_order=new_index)
        )
    else:
        conn.execute(
            dry_run_checkpoints.update()
            .where(dry_run_checkpoints.c.dry_run_id == dry_run_id)
            .values(last_order=new_index)
        )
    conn.execute(
        dry_runs.update()
        .where(dry_runs.c.id == dry_run_id)
        .values(
            processed_projects=new_index,
            status="completed" if new_index >= len(target_ids) else "running",
            completed_at=now_iso() if new_index >= len(target_ids) else None,
        )
    )
    return len(batch)


def start_dry_run(
    conn,
    *,
    draft_version_id: str,
    as_of: str,
    project_ids: list[str] | None = None,
    batch_size: int = 0,
) -> dict:
    candidate = load_version(conn, draft_version_id)
    if candidate is None:
        raise NotFound("规则草案不存在")
    if candidate["status"] not in ("draft", "approved"):
        raise InvalidState(f"版本状态为 {candidate['status']}，演练只能针对草案/已批准版本")

    mark = Watermark(conn, as_of)
    target_ids = _target_projects(conn, candidate, project_ids)
    summary = _run_summary(
        conn, candidate=candidate, as_of=as_of, target_ids=target_ids, mark=mark
    )

    # 相同输入重跑：优先复用已完成演练；崩溃遗留的同摘要运行则直接续跑。
    existing = conn.execute(
        select(dry_runs)
        .where(dry_runs.c.draft_version_id == draft_version_id)
        .where(dry_runs.c.input_summary == summary)
        .order_by(dry_runs.c.created_at)
    ).fetchall()
    completed = next((r for r in existing if r.status == "completed"), None)
    if completed is not None:
        return {"dry_run_id": completed.id, "reused": True, "status": "completed"}
    running = next((r for r in existing if r.status == "running"), None)
    if running is not None:
        dry_run_id = running.id
        created = False
    else:
        dry_run_id = new_id("run")
        conn.execute(
            dry_runs.insert().values(
                id=dry_run_id,
                draft_version_id=draft_version_id,
                as_of=as_of,
                input_summary=summary,
                status="running",
                total_projects=len(target_ids),
                processed_projects=0,
                project_ids=canonical(target_ids),
                created_at=now_iso(),
            )
        )
        conn.execute(
            dry_run_checkpoints.insert().values(dry_run_id=dry_run_id, last_order=0)
        )
        created = True

    count = _process_batch(
        conn, dry_run_id, target_ids, draft_version_id, as_of, mark, batch_size
    )
    return {
        "dry_run_id": dry_run_id,
        "reused": False,
        "created": created,
        "batch_processed": count,
        "status": get_dry_run(conn, dry_run_id)["status"],
    }


def resume_dry_run(conn, *, dry_run_id: str, batch_size: int = 0) -> dict:
    run = conn.execute(select(dry_runs).where(dry_runs.c.id == dry_run_id)).fetchone()
    if run is None:
        raise NotFound("演练不存在")
    if run.status == "completed":
        return {"dry_run_id": dry_run_id, "status": "completed", "batch_processed": 0}
    target_ids = json.loads(run.project_ids)
    mark = Watermark(conn, run.as_of)
    count = _process_batch(
        conn, dry_run_id, target_ids, run.draft_version_id, run.as_of, mark, batch_size
    )
    return {
        "dry_run_id": dry_run_id,
        "batch_processed": count,
        "status": get_dry_run(conn, dry_run_id)["status"],
    }


def get_dry_run(conn, dry_run_id: str) -> dict:
    run = conn.execute(select(dry_runs).where(dry_runs.c.id == dry_run_id)).fetchone()
    if run is None:
        raise NotFound("演练不存在")
    data = dict(run._mapping)
    data.pop("project_ids", None)
    data.pop("input_summary", None)
    rows = conn.execute(
        select(dry_run_results)
        .where(dry_run_results.c.dry_run_id == dry_run_id)
        .order_by(dry_run_results.c.project_id)
    ).fetchall()
    data["results"] = [
        {
            "project_id": r.project_id,
            "outcome": r.outcome,
            "rule_version_id": r.rule_version_id,
            "rule_selection_reason": r.rule_selection_reason,
            "from_cache": bool(r.from_cache),
            "detail": json.loads(r.detail),
        }
        for r in rows
    ]
    data["cached_count"] = sum(1 for r in rows if r.from_cache)
    data["recomputed_count"] = sum(1 for r in rows if not r.from_cache)
    return data


def pending_notifications(conn, *, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        select(outbox)
        .where(outbox.c.status == "pending")
        .order_by(outbox.c.id)
        .limit(limit)
    ).fetchall()
    return [
        {
            "id": r.id,
            "idempotency_key": r.idempotency_key,
            "recipient": r.recipient,
            "event_type": r.event_type,
            "subject": r.subject,
            "payload": json.loads(r.payload),
            "created_at": r.created_at,
        }
        for r in rows
    ]


def mark_delivered(conn, notification_id: int) -> None:
    result = conn.execute(
        outbox.update()
        .where(outbox.c.id == notification_id)
        .where(outbox.c.status == "pending")
        .values(status="sent", delivered_at=now_iso())
    )
    if result.rowcount == 0:
        raise NotFound("待发通知不存在或已投递")
