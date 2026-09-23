"""基础目录数据维护：人员、凭证、项目、岗位、人工等效项。"""

import json

from sqlalchemy import select

from .errors import Conflict, InvalidState, NotFound
from .models import (
    credentials,
    equivalences,
    persons,
    positions,
    projects,
    rule_versions,
)
from .util import new_id, now_iso


def upsert_person(conn, *, person_id: str | None, name: str, region: str) -> str:
    pid = person_id or new_id("person")
    existing = conn.execute(select(persons).where(persons.c.id == pid)).fetchone()
    if existing is None:
        conn.execute(
            persons.insert().values(id=pid, name=name, region=region, created_at=now_iso())
        )
    else:
        conn.execute(
            persons.update().where(persons.c.id == pid).values(name=name, region=region)
        )
    return pid


def issue_credential(
    conn, *, person_id: str, credential_type: str, issued_at: str,
    credential_id: str | None = None,
) -> str:
    if conn.execute(select(persons).where(persons.c.id == person_id)).fetchone() is None:
        raise NotFound("人员不存在")
    cid = credential_id or new_id("cred")
    conn.execute(
        credentials.insert().values(
            id=cid, person_id=person_id, credential_type=credential_type,
            issued_at=issued_at, revoked_at=None,
        )
    )
    return cid


def revoke_credential(conn, *, credential_id: str, revoked_at: str) -> None:
    row = conn.execute(
        select(credentials).where(credentials.c.id == credential_id)
    ).fetchone()
    if row is None:
        raise NotFound("凭证不存在")
    if row.revoked_at is not None:
        raise InvalidState("凭证已撤销")
    if revoked_at <= row.issued_at:
        raise Conflict("撤销时间不得早于签发时间")
    conn.execute(
        credentials.update()
        .where(credentials.c.id == credential_id)
        .values(revoked_at=revoked_at)
    )


def upsert_project(
    conn, *, project_id: str | None, name: str, region: str, project_type: str,
    started_at: str, status: str = "in_progress", owner_contact: str | None = None,
    attributes: dict | None = None,
) -> str:
    if status not in ("in_progress", "signed"):
        raise Conflict("项目状态只能是 in_progress / signed")
    pid = project_id or new_id("proj")
    values = dict(
        name=name, region=region, project_type=project_type, status=status,
        started_at=started_at, owner_contact=owner_contact,
        attributes=json.dumps(attributes or {}, ensure_ascii=False),
    )
    existing = conn.execute(select(projects).where(projects.c.id == pid)).fetchone()
    if existing is None:
        conn.execute(projects.insert().values(id=pid, signed_at=None, signed_rule_version_id=None, **values))
    else:
        conn.execute(projects.update().where(projects.c.id == pid).values(**values))
    return pid


def assign_position(
    conn, *, project_id: str, role: str, holder_id: str | None,
    required_credential_types: list[str] | None = None,
    position_id: str | None = None, position_order: int | None = None,
) -> str:
    if conn.execute(select(projects).where(projects.c.id == project_id)).fetchone() is None:
        raise NotFound("项目不存在")
    pos_id = position_id or new_id("pos")
    existing = conn.execute(select(positions).where(positions.c.id == pos_id)).fetchone()
    order = position_order if position_order is not None else (0 if existing is None else existing.position_order)
    values = dict(
        project_id=project_id, role=role, holder_id=holder_id, position_order=order,
        required_credential_types=json.dumps(required_credential_types or []),
    )
    if existing is None:
        conn.execute(positions.insert().values(id=pos_id, **values))
    else:
        conn.execute(positions.update().where(positions.c.id == pos_id).values(**values))
    return pos_id


def register_manual_equivalence(
    conn, *, source_type: str, target_type: str
) -> str:
    """登记无法自动裁定的等效项，状态恒为 pending，等待人工复核。"""
    eid = new_id("equiv")
    conn.execute(
        equivalences.insert().values(
            id=eid, source_type=source_type, target_type=target_type,
            mode="manual", status="pending", created_at=now_iso(),
        )
    )
    return eid


def _active_rule_at(conn, *, region: str, project_type: str, day: str):
    rows = conn.execute(
        select(rule_versions)
        .where(rule_versions.c.scope_key == f"{region}|{project_type}")
        .where(rule_versions.c.status == "published")
    ).fetchall()
    return next(
        (r for r in rows
         if r.effective_from <= day and (r.effective_to is None or r.effective_to > day)),
        None,
    )


def sign_project(conn, *, project_id: str, signed_at: str) -> dict:
    """签署完成：固定签署时点该范围的生效规则快照。"""
    row = conn.execute(select(projects).where(projects.c.id == project_id)).fetchone()
    if row is None:
        raise NotFound("项目不存在")
    if row.status == "signed":
        raise InvalidState("项目已签署，快照不可更改")
    active = _active_rule_at(
        conn, region=row.region, project_type=row.project_type, day=signed_at
    )
    snapshot_id = active.id if active else None
    conn.execute(
        projects.update()
        .where(projects.c.id == project_id)
        .values(status="signed", signed_at=signed_at, signed_rule_version_id=snapshot_id)
    )
    return {"project_id": project_id, "signed_rule_version_id": snapshot_id}
