"""固定凭证水位上的单项目裁定。

裁定输入全部来自数据库在某个 as_of 时刻的快照：
- 凭证仅计入 issued_at <= as_of 且未在 as_of（含）前撤销的；
- 已签署项目固定签署时的规则快照；
- 在办项目按草案过渡条款决定沿用旧版还是采用新版；
- 结论分为 backfill（补员）/ exempt（豁免）/ review（复核）/ compliant。
"""

import json
from datetime import date

from sqlalchemy import select

from .errors import NotFound
from .models import (
    credentials,
    equivalences,
    persons,
    positions,
    projects,
    rule_versions,
)
from .util import digest


def _day(value: str) -> date:
    return date.fromisoformat(value)


def load_version(conn, version_id: str) -> dict | None:
    if version_id is None:
        return None
    row = conn.execute(
        select(rule_versions).where(rule_versions.c.id == version_id)
    ).fetchone()
    if row is None:
        return None
    data = dict(row._mapping)
    data["body_obj"] = json.loads(data["body"])
    data["transition_obj"] = json.loads(data["transition"])
    return data


class Watermark:
    """as_of 时刻的凭证水位。"""

    def __init__(self, conn, as_of: str):
        self.as_of = as_of
        self.persons: dict[str, dict] = {}
        self.valid: dict[str, list[dict]] = {}   # person_id -> 有效凭证
        self.revoked: dict[str, list[dict]] = {}  # person_id -> 已撤销凭证
        self.manual_equivs: list[dict] = []

        for row in conn.execute(select(persons)).fetchall():
            p = dict(row._mapping)
            self.persons[p["id"]] = p

        for row in conn.execute(select(credentials)).fetchall():
            c = dict(row._mapping)
            if c["issued_at"] > as_of:
                continue  # 水位日之后签发，不计入
            if c["revoked_at"] is not None and c["revoked_at"] <= as_of:
                self.revoked.setdefault(c["person_id"], []).append(c)
            else:
                self.valid.setdefault(c["person_id"], []).append(c)

        for row in conn.execute(
            select(equivalences).where(equivalences.c.mode == "manual")
        ).fetchall():
            self.manual_equivs.append(dict(row._mapping))

    def valid_types(self, person_id: str) -> set[str]:
        return {c["credential_type"] for c in self.valid.get(person_id, [])}

    def region_fingerprint(self, region: str) -> str:
        people = sorted(
            p["id"] for p in self.persons.values() if p["region"] == region
        )
        cred_rows = []
        for pid in people:
            for c in self.valid.get(pid, []):
                cred_rows.append([pid, c["credential_type"], c["issued_at"]])
            for c in self.revoked.get(pid, []):
                cred_rows.append([pid, c["credential_type"], c["issued_at"], c["revoked_at"]])
        return digest({"as_of": self.as_of, "persons": people, "credentials": sorted(cred_rows)})

    def global_fingerprint(self) -> str:
        people = sorted(self.persons)
        cred_rows = []
        for pid in people:
            for c in self.valid.get(pid, []):
                cred_rows.append([pid, c["credential_type"], c["issued_at"]])
            for c in self.revoked.get(pid, []):
                cred_rows.append([pid, c["credential_type"], c["issued_at"], c["revoked_at"]])
        equivs = sorted(
            [e["id"], e["source_type"], e["target_type"], e["status"]]
            for e in self.manual_equivs
        )
        return digest(
            {"as_of": self.as_of, "persons": people, "credentials": sorted(cred_rows),
             "manual_equivalences": equivs}
        )


def _auto_closure(body: dict) -> dict[str, set[str]]:
    """自动等效按无向图传递闭包处理（a~b 且 b~c 则 a~c）。"""
    groups: list[set[str]] = []

    def group_of(t):
        for g in groups:
            if t in g:
                return g
        return None

    for a, b in body.get("auto_equivalences", []):
        ga, gb = group_of(a), group_of(b)
        if ga is None and gb is None:
            groups.append({a, b})
        elif ga is None:
            gb.add(a)
        elif gb is None:
            ga.add(b)
        elif ga is not gb:
            ga |= gb
            groups.remove(gb)

    closure: dict[str, set[str]] = {}
    for t in {x for pair in body.get("auto_equivalences", []) for x in pair}:
        g = group_of(t)
        closure[t] = set(g) if g else {t}
    return closure


def _satisfies(required: list[str], held_types: set[str], closure: dict[str, set[str]]) -> set[str]:
    """返回持有的、可满足要求的凭证类型集合。"""
    matches = set()
    for need in required:
        acceptable = closure.get(need, {need})
        hits = held_types & acceptable
        if hits:
            matches |= hits
    return matches


def _manual_paths(required: list[str], held_types: set[str], manual_equivs: list[dict]) -> list[dict]:
    """只能经由待裁定人工等效才能满足要求时，列出相关等效项。"""
    found = []
    for eq in manual_equivs:
        if eq["status"] != "pending":
            continue
        a, b = eq["source_type"], eq["target_type"]
        if (a in required or b in required) and (a in held_types or b in held_types):
            # 必须确实跨过“要求/持有”的边界，才是本岗位所需的等效。
            if (a in required and b in held_types) or (b in required and a in held_types):
                found.append(eq)
    return found


def _is_exempt(body: dict, role: str, attributes: dict) -> dict | None:
    for item in body.get("exemptions", []):
        if item["role"] != role:
            continue
        if all(attributes.get(k) == v for k, v in item.get("when", {}).items()):
            return item
    return None


def resolve_rule(conn, project: dict, candidate: dict, as_of: str) -> tuple[dict | None, str, str]:
    """决定项目采用哪一版规则，返回 (版本, 文字原因, 类别)。"""
    if project["status"] == "signed":
        snap = load_version(conn, project["signed_rule_version_id"])
        reason = (
            f"项目已于 {project['signed_at']} 签署完成，固定签署时规则快照"
            f"（{project['signed_rule_version_id']}），不受本次换版影响"
        )
        return snap, reason, "signed_snapshot"

    in_scope = (
        project["region"] == candidate["region"]
        and project["project_type"] == candidate["project_type"]
    )
    if not in_scope:
        # 草案范围外：沿用该项目所在范围当前有效版本。
        active = conn.execute(
            select(rule_versions)
            .where(rule_versions.c.scope_key == f"{project['region']}|{project['project_type']}")
            .where(rule_versions.c.status == "published")
        ).fetchall()
        current = next(
            (dict(r._mapping) for r in active
             if r.effective_from <= as_of and (r.effective_to is None or r.effective_to > as_of)),
            None,
        )
        if current is not None:
            current["body_obj"] = json.loads(current["body"])
            current["transition_obj"] = json.loads(current["transition"])
            return current, "项目不在草案地区/项目类型范围内，沿用当前有效规则", "out_of_scope_active"
        return None, "项目不在草案范围内且该范围暂无生效规则，按岗位自身要求核验", "out_of_scope_none"

    if _day(project["started_at"]) < _day(candidate["effective_from"]):
        if candidate["transition_obj"].get("grandfather") and candidate["supersedes_version_id"]:
            old = load_version(conn, candidate["supersedes_version_id"])
            reason = (
                f"项目在生效日 {candidate['effective_from']} 前已启动（{project['started_at']}），"
                f"按过渡条款保留旧资格，适用被替代版本 {candidate['supersedes_version_id']}"
            )
            return old, reason, "grandfathered"
        reason = (
            f"项目在生效日 {candidate['effective_from']} 前已启动（{project['started_at']}），"
            "过渡条款不保留旧资格，采用新规则草案"
        )
        return candidate, reason, "new_rule"

    return (
        candidate,
        f"项目于生效日 {candidate['effective_from']}（含）之后启动，采用新规则草案",
        "new_rule",
    )


def evaluate_project(
    conn,
    *,
    project_id: str,
    candidate_version_id: str,
    as_of: str,
    watermark: "Watermark | None" = None,
) -> dict:
    project_row = conn.execute(
        select(projects).where(projects.c.id == project_id)
    ).fetchone()
    if project_row is None:
        raise NotFound(f"项目不存在: {project_id}")
    project = dict(project_row._mapping)
    project["attributes_obj"] = json.loads(project["attributes"])

    candidate = load_version(conn, candidate_version_id)
    if candidate is None:
        raise NotFound(f"草案不存在: {candidate_version_id}")

    rule, rule_reason, rule_kind = resolve_rule(conn, project, candidate, as_of)
    body = rule["body_obj"] if rule else {"requirements": {}, "auto_equivalences": [], "exemptions": []}
    closure = _auto_closure(body)

    mark = watermark or Watermark(conn, as_of)
    position_rows = conn.execute(
        select(positions)
        .where(positions.c.project_id == project_id)
        .order_by(positions.c.position_order, positions.c.id)
    ).fetchall()

    position_results = []
    project_outcome = "compliant"

    def _escalate(level: str) -> None:
        nonlocal project_outcome
        order = {"compliant": 0, "exempt": 1, "backfill": 2, "review": 3}
        if order[level] > order[project_outcome]:
            project_outcome = level

    for prow in position_rows:
        pos = dict(prow._mapping)
        required = body.get("requirements", {}).get(pos["role"]) or json.loads(
            pos["required_credential_types"]
        )
        result = {
            "position_id": pos["id"],
            "role": pos["role"],
            "holder_id": pos["holder_id"],
            "required_credential_types": required,
            "status": None,
            "reasons": [],
            "matched_credentials": [],
            "substitutes": [],
            "manual_equivalences": [],
            "revocation_risks": [],
        }

        exempt = _is_exempt(body, pos["role"], project["attributes_obj"])
        if exempt is not None:
            result["status"] = "exempt"
            result["reasons"].append(
                f"命中豁免条款：当 {exempt.get('when')} 时角色 {pos['role']} 豁免凭证要求"
            )
            _escalate("exempt")
            position_results.append(result)
            continue

        holder = pos["holder_id"]
        if holder is None:
            result["status"] = "backfill"
            result["reasons"].append("岗位空缺，需要补员")
        else:
            held = mark.valid_types(holder)
            matches = _satisfies(required, held, closure)
            if matches:
                result["status"] = "compliant"
                result["matched_credentials"] = sorted(matches)
                result["reasons"].append("在岗人员凭证在水位日有效并满足要求")
            else:
                pending = _manual_paths(required, held, mark.manual_equivs)
                if pending:
                    result["status"] = "review"
                    result["manual_equivalences"] = [
                        {"id": e["id"], "source_type": e["source_type"], "target_type": e["target_type"]}
                        for e in pending
                    ]
                    result["reasons"].append(
                        "在岗人员凭证仅能通过待人工裁定的等效项满足要求，无法自动判定"
                    )
                else:
                    result["status"] = "backfill"
                    held_list = sorted(held) or ["无有效凭证"]
                    result["reasons"].append(
                        f"在岗人员有效凭证 {held_list} 不满足要求 {required}，换版后失去合格人员"
                    )

        # 撤销风险另行追加：曾经依赖的凭证类型在水位日前已撤销。
        if holder is not None:
            for c in mark.revoked.get(holder, []):
                acceptable = set()
                for need in required:
                    acceptable |= closure.get(need, {need})
                if c["credential_type"] in acceptable:
                    result["revocation_risks"].append(
                        {
                            "credential_id": c["id"],
                            "credential_type": c["credential_type"],
                            "revoked_at": c["revoked_at"],
                        }
                    )

        # 可用替补：同地区、非本人、水位日持有可接受凭证的人员。
        if result["status"] == "backfill":
            acceptable = set()
            for need in required:
                acceptable |= closure.get(need, {need})
            for pid, person in sorted(mark.persons.items()):
                if person["region"] != project["region"] or pid == holder:
                    continue
                cand_types = sorted(mark.valid_types(pid) & acceptable)
                if cand_types:
                    result["substitutes"].append(
                        {"person_id": pid, "name": person["name"], "credentials": cand_types}
                    )
            if not result["substitutes"]:
                result["reasons"].append("库内无符合要求的可用替补，需外部招聘或培训")
            _escalate("backfill")
        elif result["status"] == "review":
            _escalate("review")

        position_results.append(result)

    return {
        "project_id": project_id,
        "project_name": project["name"],
        "region": project["region"],
        "project_type": project["project_type"],
        "project_status": project["status"],
        "outcome": project_outcome,
        "rule_version_id": rule["id"] if rule else None,
        "rule_version_seq": rule["version_seq"] if rule else None,
        "rule_selection_reason": rule_reason,
        "rule_selection_kind": rule_kind,
        "positions": position_results,
    }


def project_input_key(
    conn,
    *,
    project_id: str,
    candidate_version_id: str,
    as_of: str,
    result: dict,
    watermark: "Watermark | None" = None,
) -> str:
    """项目相关输入切片哈希。

    规则切片只纳入该项目实际拥有的角色：草案中无关角色的条款变更不会改变本键，
    因此“局部修改只重算相关项目”。凭证水位按地区指纹纳入，人员变动同样只波及
    同地区项目。
    """
    project_row = conn.execute(
        select(projects).where(projects.c.id == project_id)
    ).fetchone()
    project = dict(project_row._mapping)
    roles = {
        r[0]
        for r in conn.execute(
            select(positions.c.role).where(positions.c.project_id == project_id)
        ).fetchall()
    }
    mark = watermark or Watermark(conn, as_of)

    position_rows = conn.execute(
        select(positions).where(positions.c.project_id == project_id)
    ).fetchall()
    pos_payload = [dict(r._mapping) for r in position_rows]

    candidate = load_version(conn, candidate_version_id)

    def rule_slice(version: dict | None) -> dict | None:
        if version is None:
            return None
        body = version["body_obj"]
        closure = _auto_closure(body)
        requirements = body.get("requirements", {})
        reachable_types: set[str] = set()
        for role in roles:
            reachable_types.update(requirements.get(role, []))
        expanded = set(reachable_types)
        for t in list(reachable_types):
            expanded |= closure.get(t, {t})
        edges = [
            pair
            for pair in body.get("auto_equivalences", [])
            if pair[0] in expanded or pair[1] in expanded
        ]
        return {
            "effective_from": version["effective_from"],
            "effective_to": version["effective_to"],
            "transition": version["transition_obj"],
            "requirements": {r: requirements.get(r, []) for r in sorted(roles)},
            "auto_equivalences": edges,
            "exemptions": [e for e in body.get("exemptions", []) if e["role"] in roles],
        }

    superseded = (
        load_version(conn, candidate["supersedes_version_id"])
        if candidate and candidate["supersedes_version_id"]
        else None
    )
    active_slice = None
    if result["rule_selection_kind"].startswith("out_of_scope"):
        active = load_version(conn, result["rule_version_id"]) if result["rule_version_id"] else None
        active_slice = rule_slice(active)

    required_types = set()
    for p in pos_payload:
        required_types.update(json.loads(p["required_credential_types"]))
    equiv_payload = [
        {k: e[k] for k in ("id", "source_type", "target_type", "status")}
        for e in mark.manual_equivs
        if e["source_type"] in required_types or e["target_type"] in required_types
    ]

    slice_ = {
        "as_of": as_of,
        "project": project,
        "positions": pos_payload,
        "region_watermark": mark.region_fingerprint(project["region"]),
        "manual_equivalences": equiv_payload,
        "rule_kind": result["rule_selection_kind"],
        "candidate": rule_slice(candidate),
        "superseded": rule_slice(superseded),
        "active": active_slice,
    }
    return digest(slice_)
