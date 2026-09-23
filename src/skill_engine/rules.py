"""资格规则草案、批准、原子生效、撤回、紧急延期。

时间线规则：每个（地区, 项目类型）范围维护一条单调追加的事件链。
发布/撤回/延期都在单事务内 CAS 推进 scope 指针并追加事件；
BEGIN IMMEDIATE 使竞争事务在库级串行，(scope_key, seq) 唯一约束兜底，
因此撤回与紧急延期并发竞争时只可能留下一条有效时间线。
"""

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .errors import BadRequest, Conflict, InvalidState, NotFound, NotAuthorized
from .models import rule_scopes, rule_timeline_events, rule_versions
from .util import digest, new_id, now_iso


def scope_key(region: str, project_type: str) -> str:
    return f"{region}|{project_type}"


def _today() -> str:
    return date.today().isoformat()


def _parse_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def _validate_body(body: dict) -> dict:
    if not isinstance(body, dict):
        raise BadRequest("规则内容必须是对象")
    requirements = body.get("requirements", {})
    if not isinstance(requirements, dict):
        raise BadRequest("requirements 必须是 {角色: [凭证类型]} 对象")
    for role, types in requirements.items():
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            raise BadRequest(f"角色 {role} 的凭证类型列表非法")
    auto = body.get("auto_equivalences", [])
    if not isinstance(auto, list) or not all(
        isinstance(pair, list) and len(pair) == 2 for pair in auto
    ):
        raise BadRequest("auto_equivalences 必须是 [来源类型, 目标类型] 列表")
    exemptions = body.get("exemptions", [])
    if not isinstance(exemptions, list):
        raise BadRequest("exemptions 必须是列表")
    for item in exemptions:
        if not isinstance(item, dict) or "role" not in item or not isinstance(item.get("when", {}), dict):
            raise BadRequest("豁免项必须包含 role 与 when")
    return {
        "requirements": requirements,
        "auto_equivalences": auto,
        "exemptions": exemptions,
    }


def _validate_transition(transition: dict | None) -> dict:
    transition = transition or {}
    grandfather = bool(transition.get("grandfather", False))
    return {"grandfather": grandfather}


def create_draft(
    conn,
    *,
    region: str,
    project_type: str,
    body: dict,
    effective_from: str,
    effective_to: str | None,
    created_by: str,
    supersedes_version_id: str | None = None,
    transition: dict | None = None,
) -> dict:
    if not region or not project_type:
        raise BadRequest("地区与项目类型必填")
    if not created_by:
        raise BadRequest("规则维护人必填")
    start = _parse_day(effective_from, "effective_from")
    end = _parse_day(effective_to, "effective_to") if effective_to else None
    if end is not None and end <= start:
        raise BadRequest("effective_to 必须晚于 effective_from")

    key = scope_key(region, project_type)
    scope = conn.execute(select(rule_scopes).where(rule_scopes.c.scope_key == key)).fetchone()
    if supersedes_version_id is None:
        supersedes_version_id = scope.active_version_id if scope else None
    if supersedes_version_id is not None:
        previous = conn.execute(
            select(rule_versions).where(rule_versions.c.id == supersedes_version_id)
        ).fetchone()
        if previous is None:
            raise NotFound("被替代的规则版本不存在")
        if previous.scope_key != key:
            raise Conflict("只能替代同一地区/项目类型范围内的旧条款")
        if previous.status not in ("published", "withdrawn"):
            raise Conflict("被替代的版本必须已发布")

    clean_body = _validate_body(body)
    clean_transition = _validate_transition(transition)
    content_hash = digest(
        {
            "region": region,
            "project_type": project_type,
            "body": clean_body,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "supersedes_version_id": supersedes_version_id,
            "transition": clean_transition,
        }
    )
    version_id = new_id("rule")
    conn.execute(
        rule_versions.insert().values(
            id=version_id,
            scope_key=key,
            region=region,
            project_type=project_type,
            status="draft",
            version_seq=None,
            content_hash=content_hash,
            body=json.dumps(clean_body, ensure_ascii=False),
            effective_from=effective_from,
            effective_to=effective_to,
            supersedes_version_id=supersedes_version_id,
            transition=json.dumps(clean_transition, ensure_ascii=False),
            created_by=created_by,
            created_at=now_iso(),
        )
    )
    return {"id": version_id, "content_hash": content_hash, "scope_key": key}


def _get_version(conn, version_id: str):
    version = conn.execute(
        select(rule_versions).where(rule_versions.c.id == version_id)
    ).fetchone()
    if version is None:
        raise NotFound("规则版本不存在")
    return version


def approve(conn, *, version_id: str, approver: str) -> None:
    version = _get_version(conn, version_id)
    if not approver:
        raise Conflict("批准人必填")
    if approver == version.created_by:
        raise NotAuthorized("发布必须由规则维护者之外的人批准")
    if version.status != "draft":
        raise InvalidState(f"草案当前状态为 {version.status}，不可批准")
    conn.execute(
        rule_versions.update()
        .where(rule_versions.c.id == version_id)
        .values(status="approved", approved_by=approver, approved_at=now_iso())
    )


def _append_event(conn, *, scope, event_type, version, actor, note, effective_to=None):
    next_seq = scope.current_seq + 1
    try:
        conn.execute(
            rule_timeline_events.insert().values(
                id=new_id("evt"),
                scope_key=scope.scope_key,
                seq=next_seq,
                event_type=event_type,
                rule_version_id=version.id,
                effective_from=version.effective_from,
                effective_to=effective_to,
                actor=actor,
                recorded_at=now_iso(),
                note=note,
            )
        )
    except IntegrityError as exc:  # 并发竞争：同一序号已被占用
        raise Conflict("规则时间线已被其他操作推进，请重试") from exc
    return next_seq


def publish(conn, *, version_id: str, actor: str) -> dict:
    version = _get_version(conn, version_id)
    if version.status != "approved":
        raise InvalidState(f"版本状态为 {version.status}，只有已批准草案可发布")

    key = version.scope_key
    scope = conn.execute(
        select(rule_scopes).where(rule_scopes.c.scope_key == key)
    ).fetchone()
    if scope is None:
        # 首次发布：惰性创建范围指针。
        conn.execute(
            rule_scopes.insert().values(
                scope_key=key,
                region=version.region,
                project_type=version.project_type,
                active_version_id=None,
                current_seq=0,
            )
        )
        scope = conn.execute(
            select(rule_scopes).where(rule_scopes.c.scope_key == key)
        ).fetchone()

    if scope.active_version_id != version.supersedes_version_id:
        raise Conflict(
            "替代基线已变化（可能存在并发发布），本草案不能生效；请基于当前版本重建草案"
        )

    next_seq = _append_event(
        conn, scope=scope, event_type="publish", version=version, actor=actor,
        note="原子切换生效",
    )

    # 旧开放区间在新版本生效日收口；已撤销或已收口的旧条款不动。
    if version.supersedes_version_id is not None:
        previous = _get_version(conn, version.supersedes_version_id)
        if previous.status == "published" and previous.effective_to is None:
            conn.execute(
                rule_versions.update()
                .where(rule_versions.c.id == previous.id)
                .values(effective_to=version.effective_from)
            )

    conn.execute(
        rule_versions.update()
        .where(rule_versions.c.id == version_id)
        .values(status="published", version_seq=next_seq, published_at=now_iso())
    )
    conn.execute(
        rule_scopes.update()
        .where(rule_scopes.c.scope_key == key)
        .values(active_version_id=version_id, current_seq=next_seq)
    )
    return {"scope_key": key, "version_seq": next_seq, "active_version_id": version_id}


def withdraw(conn, *, version_id: str, actor: str, effective_from: str | None = None) -> dict:
    version = _get_version(conn, version_id)
    if version.status != "published":
        raise InvalidState(f"版本状态为 {version.status}，只有已生效版本可撤回")
    day = effective_from or _today()
    _parse_day(day, "effective_from")

    scope = conn.execute(
        select(rule_scopes).where(rule_scopes.c.scope_key == version.scope_key)
    ).fetchone()
    if scope is None or scope.active_version_id != version_id:
        raise InvalidState("该版本已不是当前生效版本，不能撤回；历史窗口保持不变")
    # 旧条款不因撤回而复活：指针清空，时间线继续追加。
    next_seq = _append_event(
        conn, scope=scope, event_type="withdraw", version=version, actor=actor,
        effective_to=day, note="撤回生效",
    )
    conn.execute(
        rule_versions.update()
        .where(rule_versions.c.id == version_id)
        .values(status="withdrawn", effective_to=day)
    )
    conn.execute(
        rule_scopes.update()
        .where(rule_scopes.c.scope_key == version.scope_key)
        .values(active_version_id=None, current_seq=next_seq)
    )
    return {"scope_key": version.scope_key, "version_seq": next_seq}


def extend(conn, *, version_id: str, actor: str, new_effective_to: str) -> dict:
    version = _get_version(conn, version_id)
    if version.status != "published":
        raise InvalidState(f"版本状态为 {version.status}，已撤回版本不能延期")
    new_end = _parse_day(new_effective_to, "new_effective_to")
    start = _parse_day(version.effective_from, "effective_from")
    if new_end <= start:
        raise BadRequest("新止期必须晚于生效日")
    if version.effective_to is not None and new_end <= date.fromisoformat(version.effective_to):
        raise BadRequest("紧急延期只能把止期向后推")

    scope = conn.execute(
        select(rule_scopes).where(rule_scopes.c.scope_key == version.scope_key)
    ).fetchone()
    if scope is None or scope.active_version_id != version_id:
        raise InvalidState("该版本已不是当前生效版本，不能延期；历史窗口保持不变")
    next_seq = _append_event(
        conn, scope=scope, event_type="extend", version=version, actor=actor,
        effective_to=new_effective_to, note=f"紧急延期至 {new_effective_to}",
    )
    conn.execute(
        rule_versions.update()
        .where(rule_versions.c.id == version_id)
        .values(effective_to=new_effective_to)
    )
    conn.execute(
        rule_scopes.update()
        .where(rule_scopes.c.scope_key == version.scope_key)
        .values(current_seq=next_seq)
    )
    return {"scope_key": version.scope_key, "version_seq": next_seq, "effective_to": new_effective_to}


def timeline(conn, *, region: str, project_type: str) -> list[dict]:
    key = scope_key(region, project_type)
    rows = conn.execute(
        select(rule_timeline_events)
        .where(rule_timeline_events.c.scope_key == key)
        .order_by(rule_timeline_events.c.seq)
    ).fetchall()
    return [dict(row._mapping) for row in rows]
