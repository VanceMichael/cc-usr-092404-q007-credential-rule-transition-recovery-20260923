"""换版影响演练引擎。

演练在**固定凭证水位**上模拟草案在生效日的切换：

* ``start_drill`` 固化输入（草案内容 + 水位日的凭证/人员/等效项 + 在办项目），
  计算运行级 ``input_digest`` 与逐项目 ``fragment_digest``；
* 相同输入重跑直接复用既有运行结果；
* 新项目运行会复用上一运行中片段摘要未变的结果，只有相关项目重新裁定，
  且复用的决定不重复通知；
* 每个项目在独立立即事务内裁定并落检查点，长批次可从断点继续；
* 决定与通知在同一事务写入持久待发箱。
"""

import hashlib
import uuid
from datetime import date
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Engine

from ..schema import (
    credential_equivalences,
    credentials,
    drill_project_results,
    drill_runs,
    persons,
    project_positions,
    projects,
    rule_drafts,
)
from . import outbox, rules
from .clock import Clock
from .errors import ConflictError, NotFoundError, ValidationError

OUTCOMES = ("compliant", "staffing", "review", "exempt", "completed_snapshot")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _canonical(value: Any) -> str:
    return rules.canonical_json(value)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 快照


def _freeze_snapshot(snapshot: dict[str, Any], old_rule: dict[str, Any] | None = None) -> dict[str, Any]:
    """把水位快照转成可 JSON 持久化的形态（日期 → ISO 字符串）。"""
    frozen_creds: dict[str, list[dict[str, Any]]] = {}
    for pid, creds in snapshot["creds_by_person"].items():
        frozen_creds[pid] = [
            {**c, "issued_on": _iso(c["issued_on"]), "revoked_on": _iso(c["revoked_on"])}
            for c in creds
        ]
    frozen_projects = []
    for p in snapshot["projects"]:
        frozen_projects.append(
            {
                **p,
                "started_on": _iso(p["started_on"]),
                "completed_on": _iso(p["completed_on"]),
                "exemption_expires_on": _iso(p["exemption_expires_on"]),
                "positions": [
                    {**pos, "signed_on": _iso(pos["signed_on"])} for pos in p["positions"]
                ],
            }
        )
    frozen: dict[str, Any] = {
        "persons": snapshot["persons"],
        "creds_by_person": frozen_creds,
        "equiv_map": [[r, a, s] for (r, a), s in snapshot["equiv_map"].items()],
        "projects": frozen_projects,
        "active_person_ids": snapshot.get("active_person_ids", []),
    }
    if old_rule is not None:
        frozen["old_rule"] = {
            "published_id": old_rule["published_id"],
            "version": old_rule["version"],
            "personnel_requirements": old_rule["personnel_requirements"],
        }
    return frozen


def freeze_run_inputs(
    snapshot: dict[str, Any], *, draft: dict[str, Any], old_rule: dict[str, Any]
) -> dict[str, Any]:
    """持久化运行输入：水位快照 + 草案内容 + 旧规则（续跑与摘要都基于同一冻结面）。"""
    frozen = _freeze_snapshot(snapshot, old_rule)
    frozen["draft"] = {
        "draft_id": draft["draft_id"],
        "draft_version": draft["draft_version"],
        "region": draft["region"],
        "project_type": draft["project_type"],
        "new_version": draft["new_version"],
        "effective_on": _iso(draft["effective_on"]),
        "supersedes_version": draft["supersedes_version"],
        "personnel_requirements": draft["personnel_requirements"],
        "grandfather_policy": draft["grandfather_policy"],
        "content_hash": draft["content_hash"],
    }
    return frozen


def _thaw_snapshot(frozen: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    def as_date(value: Any) -> Any:
        return date.fromisoformat(value) if isinstance(value, str) else value

    persons_map = {pid: dict(p) for pid, p in frozen.get("persons", {}).items()}
    creds_by_person: dict[str, list[dict[str, Any]]] = {}
    for pid, creds in frozen.get("creds_by_person", {}).items():
        creds_by_person[pid] = [
            {**c, "issued_on": as_date(c["issued_on"]), "revoked_on": as_date(c["revoked_on"])}
            for c in creds
        ]
    equiv_map: dict[tuple[str, str], str] = {
        (r, a): s for r, a, s in frozen.get("equiv_map", [])
    }
    projects = []
    for p in frozen.get("projects", []):
        item = {
            **p,
            "started_on": as_date(p["started_on"]),
            "completed_on": as_date(p["completed_on"]),
            "exemption_expires_on": as_date(p["exemption_expires_on"]),
            "positions": [
                {
                    **pos,
                    "signed_on": as_date(pos["signed_on"]),
                }
                for pos in p.get("positions", [])
            ],
        }
        projects.append(item)
    snapshot = {
        "persons": persons_map,
        "creds_by_person": creds_by_person,
        "equiv_map": equiv_map,
        "projects": projects,
        "active_person_ids": list(frozen.get("active_person_ids", [])),
    }
    draft = frozen.get("draft") or {}
    if draft.get("effective_on"):
        draft = {**draft, "effective_on": as_date(draft["effective_on"])}
    return snapshot, frozen.get("old_rule"), draft


def _take_snapshot(
    conn, *, watermark: date, region: str, project_type: str, scope_region: str | None, scope_pt: str | None
) -> dict[str, Any]:
    region_filter = scope_region or region
    type_filter = scope_pt or project_type

    person_rows = conn.execute(select(persons)).mappings().all()
    cred_rows = conn.execute(
        select(credentials).where(credentials.c.issued_on <= watermark)
    ).mappings().all()
    equiv_rows = conn.execute(select(credential_equivalences)).mappings().all()
    project_rows = conn.execute(
        select(projects)
        .where(
            and_(
                projects.c.region == region_filter,
                projects.c.project_type == type_filter,
                projects.c.started_on <= watermark,
            )
        )
        .order_by(projects.c.project_id)
    ).mappings().all()
    position_rows = conn.execute(
        select(project_positions)
        .where(
            project_positions.c.project_id.in_([p["project_id"] for p in project_rows])
            if project_rows
            else False
        )
        .order_by(project_positions.c.position_id)
    ).mappings().all()
    # 替补是地区性人力池：同地区任何在办项目上“结论未签署”的人都算在岗
    active_rows = conn.execute(
        select(project_positions.c.person_id)
        .join(projects, projects.c.project_id == project_positions.c.project_id)
        .where(
            projects.c.region == region_filter,
            project_positions.c.signed_on.is_(None),
            projects.c.started_on <= watermark,
            or_(projects.c.completed_on.is_(None), projects.c.completed_on > watermark),
        )
    ).all()

    person_map = {p["person_id"]: dict(p) for p in person_rows}
    creds_by_person: dict[str, list[dict[str, Any]]] = {}
    for c in cred_rows:
        creds_by_person.setdefault(c["person_id"], []).append(dict(c))
    equiv_map: dict[tuple[str, str], str] = {}
    for e in equiv_rows:
        equiv_map[(e["required_code"], e["accepted_code"])] = e["status"]

    positions_by_project: dict[str, list[dict[str, Any]]] = {}
    for pos in position_rows:
        positions_by_project.setdefault(pos["project_id"], []).append(dict(pos))

    project_payloads = []
    for p in project_rows:
        item = dict(p)
        item["positions"] = sorted(
            positions_by_project.get(p["project_id"], []), key=lambda x: x["position_id"]
        )
        project_payloads.append(item)

    return {
        "persons": person_map,
        "creds_by_person": creds_by_person,
        "equiv_map": equiv_map,
        "projects": project_payloads,
        # 仍在执行（岗位结论未签署）的人员视为在岗，不能作为其他项目的替补
        "active_person_ids": sorted({r[0] for r in active_rows}),
    }


def _held_codes(creds: list[dict[str, Any]], watermark: date) -> tuple[set[str], list[dict[str, str]]]:
    """水位日有效凭证代码集合，以及被撤销凭证的风险条目。"""
    held: set[str] = set()
    revoked: list[dict[str, str]] = []
    for c in creds:
        if c["revoked_on"] is not None and c["revoked_on"] <= watermark:
            revoked.append(
                {"credential_id": c["credential_id"], "credential_code": c["credential_code"]}
            )
        else:
            held.add(c["credential_code"])
    return held, revoked


def _match_requirement(
    required_codes: list[str], held: set[str], equiv_map: dict[tuple[str, str], str]
) -> tuple[set[str], list[dict[str, str]], list[dict[str, str]]]:
    """返回（经等效后仍缺的代码、自动接受的等效、待人工裁定的等效）。

    同一必需代码若同时存在已接受与待裁定等效，优先采用已接受项。
    """
    used_equivs: list[dict[str, str]] = []
    pending_equivs: list[dict[str, str]] = []
    missing: set[str] = set()
    for code in required_codes:
        if code in held:
            continue
        accepted_codes = sorted(
            a for (r, a), s in equiv_map.items() if r == code and a in held and s == "accepted"
        )
        pending_codes = sorted(
            a for (r, a), s in equiv_map.items() if r == code and a in held and s == "pending"
        )
        if accepted_codes:
            used_equivs.append({"required_code": code, "accepted_code": accepted_codes[0]})
        elif pending_codes:
            for a in pending_codes:
                pending_equivs.append({"required_code": code, "accepted_code": a})
        else:
            missing.add(code)
    return missing, used_equivs, pending_equivs


def _candidate_projection(
    *,
    requirement: dict[str, Any],
    region: str,
    persons_map: dict[str, dict[str, Any]],
    creds_by_person: dict[str, list[dict[str, Any]]],
    equiv_map: dict[tuple[str, str], str],
    watermark: date,
    busy_person_ids: set[str],
    active_person_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """同地区可立即替补的人员（直接持证或已接受等效），忙碌者排除。

    busy 是本项目在岗人员；active 是同地区其他在办项目上未签署岗位的人员。
    """
    excluded = busy_person_ids | (active_person_ids or set())
    candidates = []
    for pid, person in sorted(persons_map.items()):
        if person["region"] != region or pid in excluded:
            continue
        held, _ = _held_codes(creds_by_person.get(pid, []), watermark)
        missing, used_equivs, pending = _match_requirement(
            requirement["required_credentials"], held, equiv_map
        )
        if not missing and not pending:
            candidates.append(
                {
                    "person_id": pid,
                    "name": person["name"],
                    "via_equivalences": used_equivs,
                }
            )
    return candidates


def _fragment_inputs(
    *,
    draft_row: dict[str, Any],
    project: dict[str, Any],
    snapshot: dict[str, Any],
    watermark: date,
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    """只收集与该项目裁定相关的输入，保证无关变化不污染片段摘要。"""
    region = project["region"]
    busy = {pos["person_id"] for pos in project["positions"]}
    required_codes = sorted({c for r in requirements for c in r["required_credentials"]})
    candidate_index = {}
    for requirement in requirements:
        candidate_index[requirement["role"]] = [
            c["person_id"]
            for c in _candidate_projection(
                requirement=requirement,
                region=region,
                persons_map=snapshot["persons"],
                creds_by_person=snapshot["creds_by_person"],
                equiv_map=snapshot["equiv_map"],
                watermark=watermark,
                busy_person_ids=busy,
                active_person_ids=set(snapshot.get("active_person_ids", [])),
            )
        ]
    assigned_creds = {}
    revocation_rows = []
    for pos in project["positions"]:
        pid = pos["person_id"]
        creds = snapshot["creds_by_person"].get(pid, [])
        assigned_creds[pid] = sorted(
            (
                {
                    "credential_code": c["credential_code"],
                    "revoked_on": _iso(c["revoked_on"]),
                }
                for c in creds
            ),
            key=lambda x: (x["credential_code"], x["revoked_on"] or ""),
        )
        for c in creds:
            if c["revoked_on"] is not None and c["revoked_on"] <= watermark:
                revocation_rows.append(
                    {"person_id": pid, "credential_code": c["credential_code"]}
                )
    equiv_rows = sorted(
        [
            {"required_code": r, "accepted_code": a, "status": s}
            for (r, a), s in snapshot["equiv_map"].items()
            if r in required_codes
        ],
        key=lambda x: (x["required_code"], x["accepted_code"]),
    )
    return {
        "requirements": requirements,
        "project": {
            "project_id": project["project_id"],
            "region": project["region"],
            "project_type": project["project_type"],
            "started_on": _iso(project["started_on"]),
            "completed_on": _iso(project["completed_on"]),
            "exemption_code": project["exemption_code"],
            "exemption_expires_on": _iso(project["exemption_expires_on"]),
            "positions": [
                {
                    "position_id": p["position_id"],
                    "role": p["role"],
                    "person_id": p["person_id"],
                    "signed_on": _iso(p["signed_on"]),
                    "signed_rule_version": p["signed_rule_version"],
                    "signed_match": p["signed_match"],
                }
                for p in project["positions"]
            ],
        },
        "assigned_credentials": assigned_creds,
        "candidate_index": candidate_index,
        "equivalences": equiv_rows,
        "revocations": sorted(revocation_rows, key=lambda x: (x["person_id"], x["credential_code"])),
    }


# --------------------------------------------------------------------------- 单项目裁定


def _evaluate_project(
    *,
    draft_row: dict[str, Any],
    old_rule: dict[str, Any] | None,
    project: dict[str, Any],
    snapshot: dict[str, Any],
    watermark: date,
) -> dict[str, Any]:
    effective_on = draft_row["effective_on"]
    new_version = draft_row["new_version"]
    reasons: list[str] = []

    signed_positions = [p for p in project["positions"] if p["signed_on"] is not None]
    unsigned_positions = [p for p in project["positions"] if p["signed_on"] is None]

    # 1) 已签署完成 / 已完工：固定原快照；撤销只追加风险，不重开结论
    if project["completed_on"] is not None and project["completed_on"] <= watermark:
        reasons.append(f"项目已于 {_iso(project['completed_on'])} 签署完成，固定签署时规则快照，不随换版重开")
        return _frozen_detail(
            draft_row=draft_row,
            project=project,
            snapshot=snapshot,
            watermark=watermark,
            outcome="completed_snapshot",
            reasons=reasons,
            frozen_version=signed_positions[0]["signed_rule_version"] if signed_positions else None,
        )
    if project["positions"] and len(signed_positions) == len(project["positions"]):
        reasons.append("项目全部岗位结论均已签署，固定签署时规则快照，不随换版重开")
        return _frozen_detail(
            draft_row=draft_row,
            project=project,
            snapshot=snapshot,
            watermark=watermark,
            outcome="completed_snapshot",
            reasons=reasons,
            frozen_version=signed_positions[0]["signed_rule_version"],
        )

    # 2) 豁免在生效日仍有效
    if project["exemption_code"] and (
        project["exemption_expires_on"] is None
        or project["exemption_expires_on"] >= effective_on
    ):
        reasons.append(
            f"豁免 {project['exemption_code']} 在生效日 {_iso(effective_on)} 仍有效"
            f"（至 {_iso(project['exemption_expires_on']) or '无截止日'}），进入豁免名单"
        )
        return {
            "applicable_version": new_version,
            "rule_effective_on": _iso(effective_on),
            "grandfather": {"retained": False, "mode": draft_row["grandfather_policy"]["mode"]},
            "outcome": "exempt",
            "reasons": reasons,
            "affected_positions": [],
            "unresolved_equivalences": [],
            "revocation_risks": [],
        }

    # 3) 过渡条款：生效前已启动的项目是否保留旧资格
    policy = draft_row["grandfather_policy"]
    retained = False
    if policy["mode"] == "project_start" and project["started_on"] < effective_on:
        cutoff = date.fromordinal(project["started_on"].toordinal() + int(policy["grace_days"]))
        if effective_on <= cutoff:
            retained = True
            reasons.append(
                f"项目启动日 {_iso(project['started_on'])} 早于生效日，按过渡条款 project_start"
                f"（宽限 {policy['grace_days']} 天，至 {_iso(cutoff)}）保留旧资格，"
                f"适用旧版 {old_rule['version'] if old_rule else draft_row['supersedes_version']}"
            )
    if not retained:
        if policy["mode"] == "project_start":
            reasons.append(
                f"项目启动日 {_iso(project['started_on'])} 的过渡宽限已过，适用新版 {new_version}"
            )
        else:
            reasons.append(f"无过渡保留条款（mode=none），生效日起适用新版 {new_version}")

    requirements = (
        old_rule["personnel_requirements"]
        if retained and old_rule is not None
        else draft_row["personnel_requirements"]
    )
    applicable_version = old_rule["version"] if retained and old_rule is not None else new_version
    req_by_role = {r["role"]: r for r in requirements}

    affected: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    revocation_risks: list[dict[str, Any]] = []
    staffed_roles = {p["role"]: p for p in unsigned_positions}

    # 新规则新增、项目尚未配人的岗位
    for role, requirement in req_by_role.items():
        if role in staffed_roles:
            continue
        candidates = _candidate_projection(
            requirement=requirement,
            region=project["region"],
            persons_map=snapshot["persons"],
            creds_by_person=snapshot["creds_by_person"],
            equiv_map=snapshot["equiv_map"],
            watermark=watermark,
            busy_person_ids={p["person_id"] for p in project["positions"]},
            active_person_ids=set(snapshot.get("active_person_ids", [])),
        )
        affected.append(
            {
                "position_id": None,
                "role": role,
                "person_id": None,
                "status": "missing_position",
                "missing_credentials": list(requirement["required_credentials"]),
                "used_equivalences": [],
                "pending_equivalences": [],
                "available_replacements": candidates,
            }
        )
        reasons.append(f"新版要求的岗位 {role} 尚在缺员，需补员")

    # 已配人但未签署的岗位逐岗核对
    for pos in unsigned_positions:
        requirement = req_by_role.get(pos["role"])
        if requirement is None:
            continue  # 旧版保留时该岗位不受旧规约束，或新版已取消该岗
        creds = snapshot["creds_by_person"].get(pos["person_id"], [])
        held, revoked = _held_codes(creds, watermark)
        person = snapshot["persons"].get(pos["person_id"], {})
        required_set = set(requirement["required_credentials"])
        missing, used_equivs, pending = _match_requirement(
            requirement["required_credentials"], held, snapshot["equiv_map"]
        )
        # 撤销风险只登记“本岗位要求、且撤销后无有效凭证或已接受等效覆盖”的凭证
        for item in revoked:
            if item["credential_code"] in required_set and item["credential_code"] in missing:
                revocation_risks.append(
                    {
                        "position_id": pos["position_id"],
                        "role": pos["role"],
                        "person_id": pos["person_id"],
                        "person_name": person.get("name"),
                        "signed_frozen": False,
                        **item,
                    }
                )
        if missing or pending:
            candidates = _candidate_projection(
                requirement=requirement,
                region=project["region"],
                persons_map=snapshot["persons"],
                creds_by_person=snapshot["creds_by_person"],
                equiv_map=snapshot["equiv_map"],
                watermark=watermark,
                busy_person_ids={p["person_id"] for p in project["positions"]},
                active_person_ids=set(snapshot.get("active_person_ids", [])),
            )
            affected.append(
                {
                    "position_id": pos["position_id"],
                    "role": pos["role"],
                    "person_id": pos["person_id"],
                    "person_name": person.get("name"),
                    "status": "ambiguous" if pending and not missing else "gap",
                    "missing_credentials": sorted(missing),
                    "used_equivalences": used_equivs,
                    "pending_equivalences": pending,
                    "available_replacements": candidates,
                }
            )
            for eq in pending:
                unresolved.append(
                    {
                        "position_id": pos["position_id"],
                        "role": pos["role"],
                        "person_id": pos["person_id"],
                        **eq,
                    }
                )
            if missing:
                names = "、".join(
                    f"{c['name']}（{c['person_id']}）" for c in candidates
                ) or "无"
                reasons.append(
                    f"岗位 {pos['role']} 现任缺少凭证 {sorted(missing)}，进入补员；可用替补：{names}"
                )
            if pending:
                reasons.append(
                    f"岗位 {pos['role']} 存在待裁定等效项 {[p['accepted_code'] for p in pending]}，进入人工复核"
                )

    # 已签署岗位的撤销风险（结论固定，风险另行追加）
    for pos in signed_positions:
        for c in snapshot["creds_by_person"].get(pos["person_id"], []):
            if c["revoked_on"] is not None and (
                pos["signed_on"] is None or c["revoked_on"] >= pos["signed_on"]
            ):
                person = snapshot["persons"].get(pos["person_id"], {})
                revocation_risks.append(
                    {
                        "position_id": pos["position_id"],
                        "role": pos["role"],
                        "person_id": pos["person_id"],
                        "person_name": person.get("name"),
                        "credential_id": c["credential_id"],
                        "credential_code": c["credential_code"],
                        "signed_frozen": True,
                    }
                )

    if any(a["status"] in ("gap", "missing_position") for a in affected):
        outcome = "staffing"
    elif unresolved:
        outcome = "review"
    else:
        outcome = "compliant"

    return {
        "applicable_version": applicable_version,
        "rule_effective_on": _iso(effective_on),
        "grandfather": {
            "retained": retained,
            "mode": policy["mode"],
            "grace_days": policy.get("grace_days"),
        },
        "outcome": outcome,
        "reasons": reasons,
        "affected_positions": affected,
        "unresolved_equivalences": unresolved,
        "revocation_risks": revocation_risks,
        "signed_positions_frozen": [
            {
                "position_id": p["position_id"],
                "role": p["role"],
                "person_id": p["person_id"],
                "signed_rule_version": p["signed_rule_version"],
                "signed_match": p["signed_match"],
            }
            for p in signed_positions
        ],
    }


def _frozen_detail(
    *,
    draft_row: dict[str, Any],
    project: dict[str, Any],
    snapshot: dict[str, Any],
    watermark: date,
    outcome: str,
    reasons: list[str],
    frozen_version: str | None,
) -> dict[str, Any]:
    revocation_risks: list[dict[str, Any]] = []
    for pos in project["positions"]:
        if pos["signed_on"] is None:
            continue
        for c in snapshot["creds_by_person"].get(pos["person_id"], []):
            if c["revoked_on"] is not None and c["revoked_on"] >= pos["signed_on"]:
                person = snapshot["persons"].get(pos["person_id"], {})
                revocation_risks.append(
                    {
                        "position_id": pos["position_id"],
                        "role": pos["role"],
                        "person_id": pos["person_id"],
                        "person_name": person.get("name"),
                        "credential_id": c["credential_id"],
                        "credential_code": c["credential_code"],
                        "signed_frozen": True,
                    }
                )
    if revocation_risks:
        reasons.append("签署后发生凭证撤销，结论保持固定，另行追加撤销风险条目")
    return {
        "applicable_version": frozen_version,
        "rule_effective_on": _iso(draft_row["effective_on"]),
        "grandfather": {"retained": False, "mode": draft_row["grandfather_policy"]["mode"]},
        "outcome": outcome,
        "reasons": reasons,
        "affected_positions": [],
        "unresolved_equivalences": [],
        "revocation_risks": revocation_risks,
        "signed_positions_frozen": [
            {
                "position_id": p["position_id"],
                "role": p["role"],
                "person_id": p["person_id"],
                "signed_rule_version": p["signed_rule_version"],
                "signed_match": p["signed_match"],
            }
            for p in project["positions"]
            if p["signed_on"] is not None
        ],
    }


# --------------------------------------------------------------------------- 演练运行


def start_drill(
    engine: Engine,
    *,
    clock: Clock,
    draft_id: str,
    watermark_date: date | str,
    scope_region: str | None = None,
    scope_project_type: str | None = None,
    batch_size: int = 100,
) -> dict[str, Any]:
    watermark = watermark_date if isinstance(watermark_date, date) else date.fromisoformat(watermark_date)
    with engine.begin() as conn:
        draft_row = conn.execute(
            select(rule_drafts).where(rule_drafts.c.draft_id == draft_id)
        ).mappings().first()
        if draft_row is None:
            raise NotFoundError(f"草案不存在：{draft_id}")
        draft = dict(draft_row)
        if draft["status"] not in ("draft", "pending_approval", "approved"):
            raise ConflictError(f"草案处于 {draft['status']} 状态，生效前后不再进行演练")
        if watermark >= draft["effective_on"]:
            raise ValidationError("凭证水位必须早于规则生效日（演练用于生效前预警）")

        old_rule = rules.get_current_rule(conn, draft["region"], draft["project_type"], watermark)
        if old_rule is None:
            raise NotFoundError(
                f"水位日 {_iso(watermark)} 在 {draft['region']}/{draft['project_type']} "
                f"没有已发布旧条款，请先 bootstrap 时间线"
            )

        snapshot = _take_snapshot(
            conn,
            watermark=watermark,
            region=draft["region"],
            project_type=draft["project_type"],
            scope_region=scope_region,
            scope_pt=scope_project_type,
        )

    # 片段摘要（草案内容指纹已间接包含在 requirements 中，此处显式带入版本与水位）
    fragments: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    for project in snapshot["projects"]:
        use_requirements = draft["personnel_requirements"]
        fragment_inputs = _fragment_inputs(
            draft_row=draft,
            project=project,
            snapshot=snapshot,
            watermark=watermark,
            requirements=use_requirements,
        )
        envelope = {
            "draft_id": draft_id,
            "draft_version": draft["draft_version"],
            "content_hash": draft["content_hash"],
            "watermark": _iso(watermark),
            "old_rule": {
                "published_id": old_rule["published_id"],
                "version": old_rule["version"],
                "personnel_requirements": old_rule["personnel_requirements"],
            },
            "inputs": fragment_inputs,
        }
        fragments.append((project["project_id"], _digest(envelope), project, fragment_inputs))

    input_digest = _digest(
        {
            "draft_id": draft_id,
            "draft_version": draft["draft_version"],
            "content_hash": draft["content_hash"],
            "watermark": _iso(watermark),
            "scope": [scope_region, scope_project_type],
            "fragments": [[pid, fd] for pid, fd, _, _ in fragments],
        }
    )

    # 相同输入：整体复用。立即写事务串行并发启动；
    # 唯一约束竞争（两个相同演练同时开始）回退为精确复用。
    try:
        with rules.immediate(engine) as conn:
            existing = conn.execute(
                select(drill_runs).where(drill_runs.c.input_digest == input_digest)
            ).mappings().first()
            if existing is not None:
                summary = _run_summary(conn, existing["run_id"])
                summary["reused"] = True
                summary["reuse_mode"] = "exact"
                return summary

            run_id = uuid.uuid4().hex
            now = clock.now()
            conn.execute(
                drill_runs.insert().values(
                    run_id=run_id,
                    draft_id=draft_id,
                    draft_version=draft["draft_version"],
                    watermark_date=watermark,
                    effective_on=draft["effective_on"],
                    scope_region=scope_region,
                    scope_project_type=scope_project_type,
                    input_digest=input_digest,
                    frozen_inputs=freeze_run_inputs(snapshot, draft=draft, old_rule=old_rule),
                    status="running",
                    total_projects=len(fragments),
                    created_at=now,
                )
            )

            # 局部修改：从同草案的上一完成运行复用未变片段
            prior = conn.execute(
                select(drill_runs)
                .where(drill_runs.c.draft_id == draft_id, drill_runs.c.run_id != run_id)
                .order_by(drill_runs.c.created_at.desc())
                .limit(1)
            ).mappings().first()
            prior_fragments: dict[str, dict[str, Any]] = {}
            if prior is not None:
                prior_rows = conn.execute(
                    select(drill_project_results).where(
                        drill_project_results.c.run_id == prior["run_id"],
                        drill_project_results.c.status == "completed",
                    )
                ).mappings().all()
                prior_fragments = {r["project_id"]: dict(r) for r in prior_rows}

            copied = 0
            for pid, fd, _project, _inputs in fragments:
                prior_result = prior_fragments.get(pid)
                if prior_result is not None and prior_result["fragment_digest"] == fd:
                    conn.execute(
                        drill_project_results.insert().values(
                            result_id=uuid.uuid4().hex,
                            run_id=run_id,
                            project_id=pid,
                            status="completed",
                            outcome=prior_result["outcome"],
                            fragment_digest=fd,
                            detail=prior_result["detail"],
                            notified=True,  # 相同决定已通知过，复用不重发
                        )
                    )
                    copied += 1
                else:
                    conn.execute(
                        drill_project_results.insert().values(
                            result_id=uuid.uuid4().hex,
                            run_id=run_id,
                            project_id=pid,
                            status="pending",
                            fragment_digest=fd,
                        )
                    )
            if copied:
                conn.execute(
                    drill_runs.update()
                    .where(drill_runs.c.run_id == run_id)
                    .values(completed_projects=copied, notified_projects=copied)
                )
    except Exception as exc:  # 唯一约束竞争：相同输入的另一运行已落库
        from sqlalchemy.exc import IntegrityError

        if not isinstance(exc, IntegrityError):
            raise
        with engine.connect() as conn:
            winner = conn.execute(
                select(drill_runs).where(drill_runs.c.input_digest == input_digest)
            ).mappings().first()
            if winner is None:
                raise
            summary = _run_summary(conn, winner["run_id"])
        summary["reused"] = True
        summary["reuse_mode"] = "exact"
        return summary

    summary = resume_drill(engine, clock=clock, run_id=run_id, limit=batch_size)
    summary["reused"] = False
    summary["reuse_mode"] = "fragment" if copied else "none"
    summary["copied_projects"] = copied
    return summary


def resume_drill(engine: Engine, *, clock: Clock, run_id: str, limit: int = 100) -> dict[str, Any]:
    """从断点继续：每条待裁项目在独立立即事务内完成“裁定 + 检查点 + 通知”。

    裁定使用启动时固化的水位快照（``frozen_inputs``），
    续跑期间实时表的变化不会污染本次演练，保证与输入摘要一致。
    """
    with engine.connect() as conn:
        row = conn.execute(
            select(drill_runs).where(drill_runs.c.run_id == run_id)
        ).mappings().first()
        if row is None:
            raise NotFoundError(f"演练运行不存在：{run_id}")
        frozen_snapshot, frozen_old_rule, frozen_draft = _thaw_snapshot(row["frozen_inputs"] or {})
    watermark = row["watermark_date"]
    if frozen_old_rule is None or not frozen_draft:
        raise NotFoundError("该运行缺少冻结输入，无法续跑")
    old_rule = frozen_old_rule
    draft = frozen_draft

    processed = 0
    while processed < limit:
        with rules.immediate(engine) as conn:
            run_row = conn.execute(
                select(drill_runs).where(drill_runs.c.run_id == run_id)
            ).mappings().first()
            pending = conn.execute(
                select(drill_project_results)
                .where(
                    drill_project_results.c.run_id == run_id,
                    drill_project_results.c.status == "pending",
                )
                .order_by(drill_project_results.c.project_id)
                .limit(1)
            ).mappings().first()
            if pending is None:
                if run_row["status"] != "completed":
                    conn.execute(
                        drill_runs.update()
                        .where(drill_runs.c.run_id == run_id)
                        .values(status="completed", completed_at=clock.now())
                    )
                break

            snapshot = frozen_snapshot
            project = next(p for p in snapshot["projects"] if p["project_id"] == pending["project_id"])
            detail = _evaluate_project(
                draft_row=draft,
                old_rule=old_rule,
                project=project,
                snapshot=snapshot,
                watermark=watermark,
            )
            outcome = detail["outcome"]
            conn.execute(
                drill_project_results.update()
                .where(drill_project_results.c.result_id == pending["result_id"])
                .values(status="completed", outcome=outcome, detail=detail)
            )
            conn.execute(
                drill_runs.update()
                .where(drill_runs.c.run_id == run_id).values(
                    completed_projects=drill_runs.c.completed_projects + 1
                )
            )

            # 决定入持久待发箱（同事务）；幂等键使续跑/重投不会产生重复决定
            project_meta = project
            idem = f"drill-decision:{run_id}:{project['project_id']}:{pending['fragment_digest']}"
            created = outbox.enqueue(
                conn,
                clock=clock,
                idempotency_key=idem,
                recipient=project_meta["manager_recipient"],
                topic="drill_decision",
                subject=_decision_subject(project, detail),
                body=_decision_body(project, detail),
                payload={
                    "run_id": run_id,
                    "project_id": project["project_id"],
                    "outcome": outcome,
                    "applicable_version": detail["applicable_version"],
                    "reasons": detail["reasons"],
                },
            )
            if created:
                conn.execute(
                    drill_project_results.update()
                    .where(drill_project_results.c.result_id == pending["result_id"])
                    .values(notified=True)
                )
                conn.execute(
                    drill_runs.update()
                    .where(drill_runs.c.run_id == run_id).values(
                        notified_projects=drill_runs.c.notified_projects + 1
                    )
                )
        processed += 1
    return get_run(engine, run_id)


def _decision_subject(project: dict[str, Any], detail: dict[str, Any]) -> str:
    labels = {
        "staffing": "需补员",
        "review": "需人工复核",
        "exempt": "豁免确认",
        "compliant": "符合适用版本要求",
        "completed_snapshot": "已签署固定快照",
    }
    return f"[换版演练] 项目 {project['name']}：{labels[detail['outcome']]}（适用 {detail['applicable_version']}）"


def _decision_body(project: dict[str, Any], detail: dict[str, Any]) -> str:
    lines = [
        f"项目：{project['name']}（{project['project_id']}）",
        f"地区/类型：{project['region']}/{project['project_type']}",
        f"适用规则版本：{detail['applicable_version']}（生效日 {detail['rule_effective_on']}）",
        f"结论：{detail['outcome']}",
        "原因：",
        *[f"- {r}" for r in detail["reasons"]],
    ]
    affected = detail.get("affected_positions", [])
    if affected:
        lines.append("受影响岗位与替补：")
        for a in affected:
            repl = "、".join(c["name"] for c in a.get("available_replacements", [])) or "无"
            lines.append(
                f"- {a['role']}：缺 {a.get('missing_credentials', [])}；可用替补：{repl}"
            )
    unresolved = detail.get("unresolved_equivalences", [])
    if unresolved:
        lines.append("待人工裁定等效项：")
        for u in unresolved:
            lines.append(f"- {u['role']}：{u['accepted_code']} ≟ {u['required_code']}")
    risks = detail.get("revocation_risks", [])
    if risks:
        lines.append("凭证撤销风险：")
        for r in risks:
            lines.append(f"- {r['role']} {r.get('person_name') or r['person_id']}：{r['credential_code']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 查询


def _run_summary(conn, run_id: str) -> dict[str, Any]:
    row = conn.execute(select(drill_runs).where(drill_runs.c.run_id == run_id)).mappings().one()
    counts = dict(
        conn.execute(
            select(drill_project_results.c.outcome, func.count(drill_project_results.c.result_id))
            .where(drill_project_results.c.run_id == run_id, drill_project_results.c.status == "completed")
            .group_by(drill_project_results.c.outcome)
        ).all()
    )
    return {
        "run_id": run_id,
        "draft_id": row["draft_id"],
        "draft_version": row["draft_version"],
        "watermark_date": _iso(row["watermark_date"]),
        "effective_on": _iso(row["effective_on"]),
        "scope": [row["scope_region"], row["scope_project_type"]],
        "input_digest": row["input_digest"],
        "status": row["status"],
        "total_projects": row["total_projects"],
        "completed_projects": row["completed_projects"],
        "notified_projects": row["notified_projects"],
        "outcome_counts": {k: counts.get(k, 0) for k in OUTCOMES},
        "created_at": row["created_at"].isoformat(),
        "completed_at": _iso(row["completed_at"]),
    }


def get_run(engine: Engine, run_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        exists = conn.execute(select(drill_runs.c.run_id).where(drill_runs.c.run_id == run_id)).first()
        if exists is None:
            raise NotFoundError(f"演练运行不存在：{run_id}")
        return _run_summary(conn, run_id)


def list_runs(engine: Engine, draft_id: str | None = None) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        stmt = select(drill_runs).order_by(drill_runs.c.created_at.desc())
        if draft_id:
            stmt = stmt.where(drill_runs.c.draft_id == draft_id)
        rows = conn.execute(stmt).mappings().all()
        return [_run_summary(conn, r["run_id"]) for r in rows]


def get_impact(engine: Engine, run_id: str) -> dict[str, Any]:
    """可直接安排补员的影响清单。"""
    with engine.connect() as conn:
        run = conn.execute(select(drill_runs).where(drill_runs.c.run_id == run_id)).mappings().first()
        if run is None:
            raise NotFoundError(f"演练运行不存在：{run_id}")
        rows = conn.execute(
            select(drill_project_results)
            .where(drill_project_results.c.run_id == run_id)
            .order_by(drill_project_results.c.project_id)
        ).mappings().all()

    projects_out: list[dict[str, Any]] = []
    affected_positions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    revocation_risks: list[dict[str, Any]] = []
    for r in rows:
        if r["status"] != "completed":
            continue
        detail = r["detail"] or {}
        projects_out.append(
            {
                "project_id": r["project_id"],
                "outcome": r["outcome"],
                "applicable_version": detail.get("applicable_version"),
                "reasons": detail.get("reasons", []),
            }
        )
        for a in detail.get("affected_positions", []):
            affected_positions.append({"project_id": r["project_id"], **a})
        for u in detail.get("unresolved_equivalences", []):
            unresolved.append({"project_id": r["project_id"], **u})
        for risk in detail.get("revocation_risks", []):
            revocation_risks.append({"project_id": r["project_id"], **risk})

    return {
        "run": get_run(engine, run_id),
        "projects": projects_out,
        "affected_positions": affected_positions,
        "unresolved_equivalences": unresolved,
        "revocation_risks": revocation_risks,
    }


def get_project_result(engine: Engine, run_id: str, project_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        run = conn.execute(select(drill_runs).where(drill_runs.c.run_id == run_id)).mappings().first()
        if run is None:
            raise NotFoundError(f"演练运行不存在：{run_id}")
        row = conn.execute(
            select(drill_project_results).where(
                drill_project_results.c.run_id == run_id,
                drill_project_results.c.project_id == project_id,
            )
        ).mappings().first()
        if row is None:
            raise NotFoundError(f"项目不在本次演练范围：{project_id}")
        project_row = conn.execute(
            select(projects).where(projects.c.project_id == project_id)
        ).mappings().first()
    if row["status"] != "completed":
        return {
            "run_id": run_id,
            "project_id": project_id,
            "status": "pending",
            "message": "该项目尚未裁定，可从断点继续演练",
        }
    return {
        "run_id": run_id,
        "project_id": project_id,
        "project_name": project_row["name"] if project_row else None,
        "status": "completed",
        **(row["detail"] or {}),
    }
