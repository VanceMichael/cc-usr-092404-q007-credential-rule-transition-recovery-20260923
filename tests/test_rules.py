"""规则草案、职责分离批准、原子生效与时间线竞争。"""

import threading

from sqlalchemy import select

from skill_engine import rules
from skill_engine.models import rule_scopes, rule_timeline_events, rule_versions

from factories import draft_rule, publish_rule


def test_draft_must_declare_scope_window_and_supersession(client):
    # 缺少地区
    response = client.post("/rules/drafts", json={
        "project_type": "光伏", "body": {"requirements": {}},
        "effective_from": "2026-10-01", "created_by": "a",
    })
    assert response.status_code == 400

    # 止期早于生效日
    response = client.post("/rules/drafts", json={
        "region": "华东", "project_type": "光伏",
        "body": {"requirements": {}},
        "effective_from": "2026-10-01", "effective_to": "2026-09-01",
        "created_by": "a",
    })
    assert response.status_code == 400

    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", created_by="maintainer-a", approver="approver-b",
    )
    draft = draft_rule(client, supersedes_version_id=v1["version_id"])
    client.post(f"/rules/{draft}/approve", json={"approver": "approver-b"})
    response = client.post(f"/rules/{draft}/publish", json={"actor": "approver-b"})
    assert response.status_code == 201

    timeline = client.get("/rules/timeline?region=华东&project_type=光伏").json()["events"]
    assert [e["event_type"] for e in timeline] == ["publish", "publish"]
    assert [e["seq"] for e in timeline] == [1, 2]


def test_approver_must_differ_from_maintainer(client):
    draft = draft_rule(client)
    response = client.post(f"/rules/{draft}/approve", json={"approver": "maintainer-a"})
    assert response.status_code == 403
    # 未批准不能发布
    response = client.post(f"/rules/{draft}/publish", json={"actor": "x"})
    assert response.status_code == 409


def test_publish_is_atomic_switch_and_closes_previous_window(engine, client):
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", created_by="m", approver="a",
    )
    v2 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C2"]}},
        effective_from="2026-10-01", created_by="m", approver="a",
        supersedes_version_id=v1["version_id"],
    )
    with engine.connect() as conn:
        scope = conn.execute(
            select(rule_scopes).where(rule_scopes.c.scope_key == "华东|光伏")
        ).fetchone()
        assert scope.active_version_id == v2["version_id"]
        old = conn.execute(
            select(rule_versions).where(rule_versions.c.id == v1["version_id"])
        ).fetchone()
        # 旧开放区间在新生效日原子收口。
        assert old.effective_to == "2026-10-01"
        assert old.status == "published"
        new = conn.execute(
            select(rule_versions).where(rule_versions.c.id == v2["version_id"])
        ).fetchone()
        assert new.status == "published"
        assert new.version_seq == 2


def test_withdraw_does_not_revive_previous_version(engine, client):
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", created_by="m", approver="a",
    )
    publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C2"]}},
        effective_from="2026-10-01", created_by="m", approver="a",
        supersedes_version_id=v1["version_id"],
    )
    # 找到 v2
    with engine.connect() as conn:
        scope = conn.execute(
            select(rule_scopes).where(rule_scopes.c.scope_key == "华东|光伏")
        ).fetchone()
        v2_id = scope.active_version_id

    response = client.post(f"/rules/{v2_id}/withdraw", json={
        "actor": "a", "effective_from": "2026-10-05",
    })
    assert response.status_code == 201

    timeline = client.get("/rules/timeline?region=华东&project_type=光伏").json()["events"]
    assert [e["event_type"] for e in timeline] == ["publish", "publish", "withdraw"]
    with engine.connect() as conn:
        scope = conn.execute(
            select(rule_scopes).where(rule_scopes.c.scope_key == "华东|光伏")
        ).fetchone()
        # 撤回 v2 不复活 v1。
        assert scope.active_version_id is None
        old = conn.execute(
            select(rule_versions).where(rule_versions.c.id == v1["version_id"])
        ).fetchone()
        assert old.effective_to == "2026-10-01"


def test_withdrawn_version_cannot_be_extended(client):
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", created_by="m", approver="a",
    )
    client.post(f"/rules/{v1['version_id']}/withdraw", json={
        "actor": "a", "effective_from": "2026-10-05",
    })
    response = client.post(f"/rules/{v1['version_id']}/extend", json={
        "actor": "a", "new_effective_to": "2026-12-31",
    })
    assert response.status_code == 409


def test_emergency_extend_only_moves_end_forward(client):
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", effective_to="2026-10-01",
        created_by="m", approver="a",
    )
    bad = client.post(f"/rules/{v1['version_id']}/extend", json={
        "actor": "a", "new_effective_to": "2026-09-01",
    })
    assert bad.status_code == 400
    ok = client.post(f"/rules/{v1['version_id']}/extend", json={
        "actor": "a", "new_effective_to": "2026-12-31",
    })
    assert ok.status_code == 201
    assert ok.json()["effective_to"] == "2026-12-31"
    timeline = client.get("/rules/timeline?region=华东&project_type=光伏").json()["events"]
    assert [e["event_type"] for e in timeline] == ["publish", "extend"]


def test_superseded_version_cannot_be_withdrawn_or_extended(engine, client):
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", created_by="m", approver="a",
    )
    publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C2"]}},
        effective_from="2026-10-01", created_by="m", approver="a",
        supersedes_version_id=v1["version_id"],
    )
    # 旧版本已被替代：撤回/延期都必须被拒绝，历史窗口不被改写。
    w = client.post(f"/rules/{v1['version_id']}/withdraw", json={
        "actor": "a", "effective_from": "2026-10-05",
    })
    assert w.status_code == 409
    e = client.post(f"/rules/{v1['version_id']}/extend", json={
        "actor": "a", "new_effective_to": "2027-01-01",
    })
    assert e.status_code == 409
    with engine.connect() as conn:
        old = conn.execute(
            select(rule_versions).where(rule_versions.c.id == v1["version_id"])
        ).fetchone()
        assert old.status == "published"
        assert old.effective_to == "2026-10-01"


def test_concurrent_withdraw_and_extend_leave_single_timeline(engine, client):
    """撤回与紧急延期竞争：库级串行 + 序号唯一约束，只留下一条有效时间线。"""
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", created_by="m", approver="a",
    )

    outcome = {}

    def race_extend():
        try:
            with engine.connect() as conn:
                with conn.begin():
                    rules.extend(
                        conn, version_id=v1["version_id"], actor="a",
                        new_effective_to="2026-12-31",
                    )
                    outcome["extend"] = "committed"
        except Exception as exc:  # 竞争失败者必须收到明确冲突，且无分叉
            outcome["extend_error"] = type(exc).__name__

    # A 先拿到写锁并撤回。
    conn_a = engine.connect()
    trans_a = conn_a.begin()
    rules.withdraw(conn_a, version_id=v1["version_id"], actor="a",
                   effective_from="2026-10-05")

    thread = threading.Thread(target=race_extend)
    thread.start()
    thread.join(timeout=2)  # B 被 IMMEDIATE 锁阻塞，拿不到时间线序号
    trans_a.commit()
    conn_a.close()
    thread.join(timeout=30)

    # 延期要么因锁失败、要么在撤回之后被状态拒绝；二者必居其一，绝不双写。
    assert outcome
    with engine.connect() as conn:
        events = conn.execute(
            select(rule_timeline_events)
            .where(rule_timeline_events.c.scope_key == "华东|光伏")
            .order_by(rule_timeline_events.c.seq)
        ).fetchall()
        assert [e.seq for e in events] == [1, 2]
        assert [e.event_type for e in events] == ["publish", "withdraw"]
        scope = conn.execute(
            select(rule_scopes).where(rule_scopes.c.scope_key == "华东|光伏")
        ).fetchone()
        assert scope.current_seq == 2
        assert scope.active_version_id is None
