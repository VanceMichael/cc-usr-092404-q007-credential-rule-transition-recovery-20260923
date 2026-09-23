"""资格规则生命周期。

一条规则适用范围由（地区, 项目类型）确定，每个范围维护唯一时间线：

* ``rule_timeline_heads`` 持有当前有效段指针，``rule_events`` 是 append-only 账本；
* 发布 / 撤回 / 紧急延期都在 ``BEGIN IMMEDIATE`` 事务内完成时间线推进，
  竞争操作只会一个接一个地读到对方已提交的状态，因此不可能产生两条有效时间线；
* 发布人必须不是规则维护者本人（维护者 ≠ 批准人）。
"""

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection, Engine

from ..schema import (
    published_rules,
    rule_drafts,
    rule_events,
    rule_timeline_heads,
    users,
)
from . import outbox
from .clock import Clock
from .errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from .transactions import immediate

VALID_DRAFT_STATUSES = {"draft", "pending_approval", "approved", "active", "superseded", "withdrawn"}
REQUIREMENT_KEYS = {"role", "required_credentials"}
GRANDFATHER_ALLOWED = {"project_start", "none"}


def head_key(region: str, project_type: str) -> str:
    return f"{region}|{project_type}"


def canonical_json(value: Any) -> str:
    """按键排序的紧凑 JSON，保证语义相同的草案摘要一致。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_fingerprint(content: dict[str, Any]) -> str:
    payload = {
        "region": content["region"],
        "project_type": content["project_type"],
        "new_version": content["new_version"],
        "effective_on": _iso(content["effective_on"]),
        "end_on": _iso(content.get("end_on")),
        "supersedes_version": content["supersedes_version"],
        "personnel_requirements": content["personnel_requirements"],
        "grandfather_policy": content["grandfather_policy"],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def validate_content(content: dict[str, Any]) -> dict[str, Any]:
    required = [
        "region",
        "project_type",
        "new_version",
        "effective_on",
        "supersedes_version",
        "personnel_requirements",
        "grandfather_policy",
    ]
    missing = [key for key in required if key not in content]
    if missing:
        raise ValidationError(f"规则草案缺少字段：{', '.join(missing)}")
    region = str(content["region"]).strip()
    project_type = str(content["project_type"]).strip()
    new_version = str(content["new_version"]).strip()
    supersedes_version = str(content["supersedes_version"]).strip()
    if not region or not project_type or not new_version or not supersedes_version:
        raise ValidationError("地区、项目类型、新版本号与被替代版本号均不能为空")
    effective_on = _as_date(content["effective_on"], "effective_on")
    end_on = _as_date(content.get("end_on"), "end_on") if content.get("end_on") else None
    if end_on is not None and end_on < effective_on:
        raise ValidationError("失效日不能早于生效日")

    requirements = content["personnel_requirements"]
    if not isinstance(requirements, list) or not requirements:
        raise ValidationError("personnel_requirements 必须是非空岗位要求列表")
    roles: set[str] = set()
    for item in requirements:
        if not isinstance(item, dict) or not REQUIREMENT_KEYS.issubset(item):
            raise ValidationError("每个岗位要求必须包含 role 与 required_credentials")
        role = str(item["role"]).strip()
        creds = item["required_credentials"]
        if not role or not isinstance(creds, list) or not all(isinstance(c, str) and c for c in creds):
            raise ValidationError("岗位名与凭证代码列表均不能为空")
        if role in roles:
            raise ValidationError(f"岗位要求重复：{role}")
        roles.add(role)

    policy = content["grandfather_policy"]
    if not isinstance(policy, dict) or policy.get("mode") not in GRANDFATHER_ALLOWED:
        raise ValidationError("grandfather_policy.mode 只能是 project_start 或 none")
    if policy["mode"] == "project_start":
        days = policy.get("grace_days")
        if not isinstance(days, int) or days < 0:
            raise ValidationError("project_start 过渡条款必须给出非负整数 grace_days")

    return {
        "region": region,
        "project_type": project_type,
        "new_version": new_version,
        "effective_on": effective_on,
        "end_on": end_on,
        "supersedes_version": supersedes_version,
        "personnel_requirements": requirements,
        "grandfather_policy": policy,
    }


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")


def _require_user(conn: Connection, user_id: str) -> dict[str, Any]:
    row = conn.execute(select(users).where(users.c.user_id == user_id)).mappings().first()
    if row is None:
        raise NotFoundError(f"用户不存在：{user_id}")
    return dict(row)


# --------------------------------------------------------------------------- 草案


def create_draft(engine: Engine, *, clock: Clock, maintainer: str, content: dict[str, Any]) -> dict[str, Any]:
    clean = validate_content(content)
    with engine.begin() as conn:
        user = _require_user(conn, maintainer)
        if not user["is_rule_maintainer"]:
            raise PermissionDeniedError("只有规则维护者可以登记草案")
        draft_id = uuid.uuid4().hex
        now = clock.now()
        conn.execute(
            rule_drafts.insert().values(
                draft_id=draft_id,
                draft_version=1,
                status="draft",
                maintainer=maintainer,
                content_hash=content_fingerprint(clean),
                change_note=str(content.get("change_note", "")),
                created_at=now,
                updated_at=now,
                **clean,
            )
        )
    return {"draft_id": draft_id, "draft_version": 1, "status": "draft", **clean}


def update_draft(
    engine: Engine, *, clock: Clock, maintainer: str, draft_id: str, content: dict[str, Any]
) -> dict[str, Any]:
    clean = validate_content(content)
    with immediate(engine) as conn:
        user = _require_user(conn, maintainer)
        if not user["is_rule_maintainer"]:
            raise PermissionDeniedError("只有规则维护者可以修改草案")
        row = conn.execute(select(rule_drafts).where(rule_drafts.c.draft_id == draft_id)).mappings().first()
        if row is None:
            raise NotFoundError(f"草案不存在：{draft_id}")
        if row["status"] != "draft":
            raise ConflictError(f"草案处于 {row['status']} 状态，不能修改（请用新版本登记）")
        next_version = row["draft_version"] + 1
        conn.execute(
            rule_drafts.update()
            .where(rule_drafts.c.draft_id == draft_id)
            .values(
                draft_version=next_version,
                status="draft",
                content_hash=content_fingerprint(clean),
                change_note=str(content.get("change_note", "")),
                updated_at=clock.now(),
                **clean,
            )
        )
    return get_draft(engine, draft_id)


def submit_for_approval(engine: Engine, *, clock: Clock, maintainer: str, draft_id: str) -> dict[str, Any]:
    with engine.begin() as conn:
        user = _require_user(conn, maintainer)
        if not user["is_rule_maintainer"]:
            raise PermissionDeniedError("只有规则维护者可以提交审批")
        row = _locked_draft(conn, draft_id)
        if row["status"] not in ("draft", "pending_approval"):
            raise ConflictError(f"草案处于 {row['status']} 状态，不能提交审批")
        conn.execute(
            rule_drafts.update()
            .where(rule_drafts.c.draft_id == draft_id)
            .values(status="pending_approval", updated_at=clock.now())
        )
    return get_draft(engine, draft_id)


def get_draft(engine: Engine, draft_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(rule_drafts).where(rule_drafts.c.draft_id == draft_id)).mappings().first()
    if row is None:
        raise NotFoundError(f"草案不存在：{draft_id}")
    return _serialize_draft(dict(row))


def list_drafts(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(rule_drafts).order_by(rule_drafts.c.created_at)).mappings().all()
    return [_serialize_draft(dict(r)) for r in rows]


def _locked_draft(conn: Connection, draft_id: str) -> dict[str, Any]:
    row = conn.execute(select(rule_drafts).where(rule_drafts.c.draft_id == draft_id)).mappings().first()
    if row is None:
        raise NotFoundError(f"草案不存在：{draft_id}")
    return dict(row)


def _serialize_draft(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_id": row["draft_id"],
        "draft_version": row["draft_version"],
        "status": row["status"],
        "region": row["region"],
        "project_type": row["project_type"],
        "new_version": row["new_version"],
        "effective_on": _iso(row["effective_on"]),
        "end_on": _iso(row["end_on"]),
        "supersedes_version": row["supersedes_version"],
        "personnel_requirements": row["personnel_requirements"],
        "grandfather_policy": row["grandfather_policy"],
        "content_hash": row["content_hash"],
        "maintainer": row["maintainer"],
        "approved_by": row["approved_by"],
        "change_note": row["change_note"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


# --------------------------------------------------------------------------- 时间线


def bootstrap_timeline(
    engine: Engine,
    *,
    clock: Clock,
    region: str,
    project_type: str,
    version: str,
    start_on: date | str,
    personnel_requirements: list[dict[str, Any]],
    grandfather_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """登记当前仍在执行的旧条款，作为时间线起点（不发送通知）。"""
    start = _as_date(start_on, "start_on")
    key = head_key(region, project_type)
    with immediate(engine) as conn:
        existing = conn.execute(
            select(rule_timeline_heads).where(rule_timeline_heads.c.head_key == key)
        ).first()
        if existing is not None:
            raise ConflictError(f"时间线已存在：{key}")
        published_id = uuid.uuid4().hex
        now = clock.now()
        conn.execute(
            published_rules.insert().values(
                published_id=published_id,
                region=region,
                project_type=project_type,
                version=version,
                start_on=start,
                end_on=None,
                supersedes_version=None,
                personnel_requirements=personnel_requirements,
                grandfather_policy=grandfather_policy or {"mode": "none"},
                source_draft_id=None,
                status="current",
                published_at=now,
            )
        )
        conn.execute(
            rule_timeline_heads.insert().values(
                head_key=key,
                region=region,
                project_type=project_type,
                current_published_id=published_id,
                tail_end_on=None,
            )
        )
        _append_event(
            conn,
            clock=clock,
            key=key,
            event_type="bootstrapped",
            published_id=published_id,
            version=version,
            actor="system",
            payload={"start_on": _iso(start)},
        )
    return get_timeline(engine, region, project_type)


def _append_event(
    conn: Connection,
    *,
    clock: Clock,
    key: str,
    event_type: str,
    published_id: str | None,
    version: str | None,
    actor: str,
    payload: dict[str, Any],
) -> int:
    next_seq = (
        conn.execute(
            select(func.coalesce(func.max(rule_events.c.seq), 0)).where(rule_events.c.head_key == key)
        ).scalar_one()
        + 1
    )
    conn.execute(
        rule_events.insert().values(
            event_id=uuid.uuid4().hex,
            head_key=key,
            seq=next_seq,
            event_type=event_type,
            published_id=published_id,
            version=version,
            actor=actor,
            payload=payload,
            occurred_at=clock.now(),
        )
    )
    return next_seq


def get_current_rule(conn: Connection, region: str, project_type: str, on_date: date) -> dict[str, Any] | None:
    """取某范围在给定日期有效的规则段；范围尚未建立时间线时返回 None。"""
    row = conn.execute(
        select(published_rules)
        .where(
            and_(
                published_rules.c.region == region,
                published_rules.c.project_type == project_type,
                published_rules.c.start_on <= on_date,
            )
        )
        .order_by(published_rules.c.start_on.desc(), published_rules.c.published_at.desc())
        .limit(1)
    ).mappings().first()
    if row is None:
        return None
    rule = dict(row)
    if rule["end_on"] is not None and not (rule["start_on"] <= on_date <= rule["end_on"]):
        return None
    return rule


def get_timeline(engine: Engine, region: str, project_type: str) -> dict[str, Any]:
    key = head_key(region, project_type)
    with engine.connect() as conn:
        head = conn.execute(
            select(rule_timeline_heads).where(rule_timeline_heads.c.head_key == key)
        ).mappings().first()
        segments = conn.execute(
            select(published_rules)
            .where(
                and_(
                    published_rules.c.region == region,
                    published_rules.c.project_type == project_type,
                )
            )
            .order_by(published_rules.c.start_on, published_rules.c.published_at)
        ).mappings().all()
        events = conn.execute(
            select(rule_events).where(rule_events.c.head_key == key).order_by(rule_events.c.seq)
        ).mappings().all()
    return {
        "head_key": key,
        "region": region,
        "project_type": project_type,
        "current_published_id": head["current_published_id"] if head else None,
        "tail_end_on": _iso(head["tail_end_on"]) if head else None,
        "segments": [_serialize_rule(dict(s)) for s in segments],
        "events": [
            {
                "seq": e["seq"],
                "event_type": e["event_type"],
                "version": e["version"],
                "actor": e["actor"],
                "payload": e["payload"],
                "occurred_at": e["occurred_at"].isoformat(),
            }
            for e in events
        ],
    }


def _serialize_rule(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "published_id": row["published_id"],
        "region": row["region"],
        "project_type": row["project_type"],
        "version": row["version"],
        "start_on": _iso(row["start_on"]),
        "end_on": _iso(row["end_on"]),
        "supersedes_version": row["supersedes_version"],
        "personnel_requirements": row["personnel_requirements"],
        "grandfather_policy": row["grandfather_policy"],
        "status": row["status"],
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
    }


# --------------------------------------------------------------------------- 审批 / 发布


def approve_draft(engine: Engine, *, clock: Clock, approver: str, draft_id: str) -> dict[str, Any]:
    """维护者之外的人批准草案；批准本身不改变时间线，发布才是原子切换。"""
    with immediate(engine) as conn:
        user = _require_user(conn, approver)
        if user["is_rule_maintainer"]:
            raise PermissionDeniedError("发布必须由规则维护者之外的人批准")
        draft = _locked_draft(conn, draft_id)
        if draft["status"] not in ("pending_approval", "approved"):
            raise ConflictError(f"草案处于 {draft['status']} 状态，不能批准")
        conn.execute(
            rule_drafts.update()
            .where(rule_drafts.c.draft_id == draft_id)
            .values(status="approved", approved_by=approver, approved_at=clock.now(), updated_at=clock.now())
        )
    return get_draft(engine, draft_id)


def publish_draft(engine: Engine, *, clock: Clock, actor: str, draft_id: str) -> dict[str, Any]:
    """正式生效：一次原子切换。

    同一事务内：旧段在新生效日前一天截断、新段写入、时间线头指针移动、
    草案置 active、事件追加、时间线变更通知入待发箱。
    """
    with immediate(engine) as conn:
        user = _require_user(conn, actor)
        draft = _locked_draft(conn, draft_id)
        if draft["status"] != "approved":
            raise ConflictError(f"草案处于 {draft['status']} 状态，只有已批准草案可以发布")
        if draft["approved_by"] == draft["maintainer"]:
            # 正常流程走不到，这里是防线
            raise PermissionDeniedError("批准人与维护者不能是同一人")

        key = head_key(draft["region"], draft["project_type"])
        head = conn.execute(
            select(rule_timeline_heads).where(rule_timeline_heads.c.head_key == key)
        ).mappings().first()
        if head is None:
            raise NotFoundError(f"时间线尚未初始化：{key}，请先登记旧条款")
        if head["current_published_id"] is None:
            raise ConflictError("该范围时间线已撤回收尾，不能直接发布；请先紧急延期恢复")

        current = conn.execute(
            select(published_rules).where(
                published_rules.c.published_id == head["current_published_id"]
            )
        ).mappings().first()
        if current is None:
            raise ConflictError("时间线指针指向的规则段缺失")
        effective_on = draft["effective_on"]
        if current["end_on"] is not None and current["end_on"] < effective_on:
            raise ConflictError("当前时间线在新规则生效前已收尾，存在空档；请先用紧急延期消除空档")
        if current["start_on"] >= effective_on:
            raise ConflictError("新生效日必须晚于当前规则段的起始日")
        if current["version"] != draft["supersedes_version"]:
            raise ConflictError(
                f"草案声明替代 {draft['supersedes_version']}，但当前有效版本是 {current['version']}；"
                "时间线必须连续，不能跳过中间版本"
            )

        # 版本号在同一范围内唯一
        dup = conn.execute(
            select(published_rules.c.published_id).where(
                and_(
                    published_rules.c.region == draft["region"],
                    published_rules.c.project_type == draft["project_type"],
                    published_rules.c.version == draft["new_version"],
                )
            )
        ).first()
        if dup is not None:
            raise ConflictError(f"版本号已存在：{draft['new_version']}")

        published_id = uuid.uuid4().hex
        now = clock.now()
        # 1) 旧段截断到生效日前一天
        cutoff = date.fromordinal(effective_on.toordinal() - 1)
        conn.execute(
            published_rules.update()
            .where(published_rules.c.published_id == current["published_id"])
            .values(end_on=cutoff, status="superseded")
        )
        # 2) 新段写入（开放区间，end_on=None）
        conn.execute(
            published_rules.insert().values(
                published_id=published_id,
                region=draft["region"],
                project_type=draft["project_type"],
                version=draft["new_version"],
                start_on=effective_on,
                end_on=draft["end_on"],
                supersedes_version=current["version"],
                personnel_requirements=draft["personnel_requirements"],
                grandfather_policy=draft["grandfather_policy"],
                source_draft_id=draft_id,
                status="current",
                published_at=now,
            )
        )
        # 3) 指针移动
        conn.execute(
            rule_timeline_heads.update()
            .where(rule_timeline_heads.c.head_key == key)
            .values(current_published_id=published_id, tail_end_on=draft["end_on"])
        )
        # 4) 草案状态
        conn.execute(
            rule_drafts.update()
            .where(rule_drafts.c.draft_id == draft_id)
            .values(status="active", updated_at=now)
        )
        # 5) 事件账本
        seq = _append_event(
            conn,
            clock=clock,
            key=key,
            event_type="activated",
            published_id=published_id,
            version=draft["new_version"],
            actor=actor,
            payload={
                "effective_on": _iso(effective_on),
                "supersedes_version": current["version"],
                "change_note": draft["change_note"],
            },
        )
        # 6) 时间线变更通知（同事务，按订阅扇出，重启不丢）
        outbox.fanout(
            conn,
            clock=clock,
            region=draft["region"],
            project_type=draft["project_type"],
            idempotency_prefix=f"rule:activated:{published_id}:{seq}",
            topic="rule_timeline",
            subject=f"规则 {draft['new_version']} 将于 {_iso(effective_on)} 生效",
            body=(
                f"{draft['region']}/{draft['project_type']} 规则 {draft['new_version']} "
                f"替代 {current['version']}，{_iso(effective_on)} 起生效。{draft['change_note']}"
            ),
            payload={
                "region": draft["region"],
                "project_type": draft["project_type"],
                "version": draft["new_version"],
                "supersedes_version": current["version"],
                "effective_on": _iso(effective_on),
                "event": "activated",
                "seq": seq,
            },
        )
    return get_timeline(engine, draft["region"], draft["project_type"])


def withdraw_rule(
    engine: Engine,
    *,
    clock: Clock,
    actor: str,
    region: str,
    project_type: str,
    withdraw_on: date | str,
    reason: str = "",
) -> dict[str, Any]:
    """撤回当前规则：时间线在 withdraw_on 收尾（之后无规则有效）。"""
    withdraw_date = _as_date(withdraw_on, "withdraw_on")
    key = head_key(region, project_type)
    with immediate(engine) as conn:
        _require_user(conn, actor)
        head = conn.execute(
            select(rule_timeline_heads).where(rule_timeline_heads.c.head_key == key)
        ).mappings().first()
        if head is None or head["current_published_id"] is None:
            raise ConflictError("该范围没有可撤回的有效规则")
        current = conn.execute(
            select(published_rules).where(
                published_rules.c.published_id == head["current_published_id"]
            )
        ).mappings().first()
        if withdraw_date < current["start_on"]:
            raise ValidationError("撤回日不能早于当前规则起始日")
        end_on = withdraw_date
        if current["end_on"] is not None:
            end_on = min(current["end_on"], withdraw_date)
        conn.execute(
            published_rules.update()
            .where(published_rules.c.published_id == current["published_id"])
            .values(end_on=end_on, status="withdrawn")
        )
        conn.execute(
            rule_timeline_heads.update()
            .where(rule_timeline_heads.c.head_key == key)
            .values(current_published_id=None, tail_end_on=end_on)
        )
        seq = _append_event(
            conn,
            clock=clock,
            key=key,
            event_type="withdrew",
            published_id=current["published_id"],
            version=current["version"],
            actor=actor,
            payload={"withdraw_on": _iso(end_on), "reason": reason},
        )
        outbox.fanout(
            conn,
            clock=clock,
            region=region,
            project_type=project_type,
            idempotency_prefix=f"rule:withdrew:{current['published_id']}:{seq}",
            topic="rule_timeline",
            subject=f"规则 {current['version']} 撤回，{_iso(end_on)} 后无有效条款",
            body=f"{region}/{project_type} 规则撤回，收尾日 {_iso(end_on)}。原因：{reason}",
            payload={
                "region": region,
                "project_type": project_type,
                "version": current["version"],
                "event": "withdrew",
                "seq": seq,
                "end_on": _iso(end_on),
            },
        )
    return get_timeline(engine, region, project_type)


def emergency_extend(
    engine: Engine,
    *,
    clock: Clock,
    actor: str,
    region: str,
    project_type: str,
    new_end_on: date | str,
    reason: str = "",
) -> dict[str, Any]:
    """紧急延期：把当前段（或刚撤回的段）的截止日后推。

    与撤回/发布竞争时依靠 IMMEDIATE 串行：后到者基于已提交的最新指针操作，
    事件账本按 seq 记录唯一先后顺序。
    """
    new_end = _as_date(new_end_on, "new_end_on")
    key = head_key(region, project_type)
    with immediate(engine) as conn:
        _require_user(conn, actor)
        head = conn.execute(
            select(rule_timeline_heads).where(rule_timeline_heads.c.head_key == key)
        ).mappings().first()
        if head is None:
            raise NotFoundError(f"时间线尚未初始化：{key}")

        if head["current_published_id"] is not None:
            target = conn.execute(
                select(published_rules).where(
                    published_rules.c.published_id == head["current_published_id"]
                )
            ).mappings().first()
        else:
            # 已撤回：取该范围最近一段进行恢复
            target = conn.execute(
                select(published_rules)
                .where(
                    and_(
                        published_rules.c.region == region,
                        published_rules.c.project_type == project_type,
                    )
                )
                .order_by(published_rules.c.start_on.desc(), published_rules.c.published_at.desc())
                .limit(1)
            ).mappings().first()
        if target is None:
            raise ConflictError("该范围没有可延期的规则段")
        if new_end < target["start_on"]:
            raise ValidationError("延期截止日不能早于规则起始日")
        if target["end_on"] is not None and new_end <= target["end_on"]:
            raise ConflictError(f"紧急延期只能后推：当前截止日为 {_iso(target['end_on'])}")

        conn.execute(
            published_rules.update()
            .where(published_rules.c.published_id == target["published_id"])
            .values(end_on=new_end, status="current")
        )
        conn.execute(
            rule_timeline_heads.update()
            .where(rule_timeline_heads.c.head_key == key)
            .values(current_published_id=target["published_id"], tail_end_on=new_end)
        )
        seq = _append_event(
            conn,
            clock=clock,
            key=key,
            event_type="extended",
            published_id=target["published_id"],
            version=target["version"],
            actor=actor,
            payload={"new_end_on": _iso(new_end), "reason": reason},
        )
        outbox.fanout(
            conn,
            clock=clock,
            region=region,
            project_type=project_type,
            idempotency_prefix=f"rule:extended:{target['published_id']}:{seq}",
            topic="rule_timeline",
            subject=f"规则 {target['version']} 紧急延期至 {_iso(new_end)}",
            body=f"{region}/{project_type} 规则紧急延期，新截止日 {_iso(new_end)}。原因：{reason}",
            payload={
                "region": region,
                "project_type": project_type,
                "version": target["version"],
                "event": "extended",
                "seq": seq,
                "new_end_on": _iso(new_end),
            },
        )
    return get_timeline(engine, region, project_type)


def list_events(engine: Engine, region: str, project_type: str) -> list[dict[str, Any]]:
    return get_timeline(engine, region, project_type)["events"]
