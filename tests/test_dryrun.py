"""演练影响清单、输入摘要复用、局部重算、断点续跑与待发箱。"""

from factories import (
    draft_rule,
    make_credential,
    make_equivalence,
    make_person,
    make_position,
    make_project,
    publish_rule,
)


def _two_projects(client):
    """p1 安全员持 C1（新规要 C2 → 补员）；p2 审核员持 C2（合规）。"""
    alice = make_person(client, name="Alice", region="华东", person_id="alice")
    bob = make_person(client, name="Bob", region="华东", person_id="bob")
    make_credential(client, person_id=alice, credential_type="C1", issued_at="2025-01-01")
    make_credential(client, person_id=bob, credential_type="C2", issued_at="2025-01-01")

    p1 = make_project(client, name="P1", region="华东", project_type="光伏",
                      started_at="2026-03-01", project_id="p1",
                      owner_contact="owner1@green")
    make_position(client, project_id="p1", role="安全员", holder_id="alice")
    p2 = make_project(client, name="P2", region="华东", project_type="光伏",
                      started_at="2026-03-01", project_id="p2")
    make_position(client, project_id="p2", role="审核员", holder_id="bob")
    # 范围外项目不应进入默认批次。
    make_project(client, name="P3", region="华北", project_type="光伏",
                 started_at="2026-03-01", project_id="p3")
    return p1, p2


def _body():
    return {
        "requirements": {"安全员": ["C2"], "审核员": ["C2"]},
        "auto_equivalences": [], "exemptions": [],
    }


def test_dry_run_lists_impacted_positions_and_substitutes(client):
    p1, p2 = _two_projects(client)
    draft = draft_rule(client, body=_body())
    started = client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    })
    assert started.status_code == 201, started.text
    run_id = started.json()["dry_run_id"]
    run = client.get(f"/dry-runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["total_projects"] == 2
    by_project = {r["project_id"]: r for r in run["results"]}
    assert by_project["p1"]["outcome"] == "backfill"
    assert by_project["p2"]["outcome"] == "compliant"
    p1_detail = by_project["p1"]["detail"]["positions"][0]
    assert p1_detail["substitutes"][0]["person_id"] == "bob"


def test_same_draft_rerun_reuses_result(client):
    _two_projects(client)
    draft = draft_rule(client, body=_body())
    first = client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    second = client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert second["reused"] is True
    assert second["dry_run_id"] == first["dry_run_id"]


def test_partial_change_recomputes_only_related_projects(client):
    _two_projects(client)
    draft1 = draft_rule(client, body=_body())
    run1 = client.post("/dry-runs", json={
        "draft_version_id": draft1, "as_of": "2026-09-23",
    }).json()
    full = client.get(f"/dry-runs/{run1['dry_run_id']}").json()
    assert full["recomputed_count"] == 2

    # 局部修改：只调整“审核员”角色要求（增加新等效），安全员条款不动。
    changed_body = {
        "requirements": {"安全员": ["C2"], "审核员": ["C2", "C3"]},
        "auto_equivalences": [], "exemptions": [],
    }
    draft2 = draft_rule(client, body=changed_body)
    run2 = client.post("/dry-runs", json={
        "draft_version_id": draft2, "as_of": "2026-09-23",
    }).json()
    data = client.get(f"/dry-runs/{run2['dry_run_id']}").json()
    by_project = {r["project_id"]: r for r in data["results"]}
    # p1（安全员）切片未变 → 命中缓存；p2（审核员）条款已变 → 重新计算。
    assert by_project["p1"]["from_cache"] is True
    assert by_project["p2"]["from_cache"] is False


def test_long_batch_resumes_from_checkpoint(client):
    _two_projects(client)
    draft = draft_rule(client, body=_body())
    first = client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23", "batch_size": 1,
    }).json()
    assert first["status"] == "running"
    run_id = first["dry_run_id"]
    partial = client.get(f"/dry-runs/{run_id}").json()
    assert partial["processed_projects"] == 1
    assert len(partial["results"]) == 1

    # 从断点继续。
    resumed = client.post(f"/dry-runs/{run_id}/resume", json={"batch_size": 1})
    assert resumed.status_code == 201
    final = client.get(f"/dry-runs/{run_id}").json()
    assert final["status"] == "completed"
    assert final["processed_projects"] == 2
    # 已处理结果不重复、不丢失。
    assert [r["project_id"] for r in final["results"]] == ["p1", "p2"]

    # 完成后续跑是空操作。
    again = client.post(f"/dry-runs/{run_id}/resume", json={"batch_size": 1}).json()
    assert again["batch_processed"] == 0


def test_notifications_persisted_and_idempotent(client):
    _two_projects(client)
    draft = draft_rule(client, body=_body())
    client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    })
    pending = client.get("/notifications").json()["pending"]
    # 仅 p1 需要补员，产生且仅产生一条决定通知；合规项目不打扰收件人。
    decisions = [n for n in pending if n["event_type"] == "skill.impact.decision"]
    assert len(decisions) == 1
    assert decisions[0]["recipient"] == "owner1@green"
    assert decisions[0]["payload"]["outcome"] == "backfill"

    # 重启式重跑（相同输入复用演练，不产生重复通知）。
    client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    })
    pending2 = client.get("/notifications").json()["pending"]
    assert len(pending2) == 1

    # 投递后从待发箱移除，重复投递不存在的记录返回 404。
    delivered = client.post(f"/notifications/{decisions[0]['id']}/deliver")
    assert delivered.status_code == 201
    assert client.get("/notifications").json()["pending"] == []
    dup = client.post(f"/notifications/{decisions[0]['id']}/deliver")
    assert dup.status_code == 404


def test_revocation_creates_separate_risk_notification(client):
    alice = make_person(client, name="Alice", region="华东", person_id="alice")
    c1 = make_credential(client, person_id=alice, credential_type="C2",
                         issued_at="2025-01-01", credential_id="c1")
    make_project(client, name="P", region="华东", project_type="光伏",
                 started_at="2026-03-01", project_id="p1")
    make_position(client, project_id="p1", role="安全员", holder_id="alice")
    draft = draft_rule(client, body={"requirements": {"安全员": ["C2"]}})
    client.post("/dry-runs", json={"draft_version_id": draft, "as_of": "2026-09-23"})
    assert client.get("/notifications").json()["pending"] == []

    # 撤销凭证 → 输入切片变化 → 重算产生补员决定 + 独立撤销风险通知。
    client.post(f"/admin/credentials/{c1}/revoke", json={"revoked_at": "2026-09-20"})
    client.post("/dry-runs", json={"draft_version_id": draft, "as_of": "2026-09-23"})
    pending = client.get("/notifications").json()["pending"]
    kinds = {n["event_type"] for n in pending}
    assert kinds == {"skill.impact.decision", "skill.impact.revocation_risk"}


def test_unresolved_manual_equivalence_appears_in_review_run(client):
    a = make_person(client, name="A", region="华东", person_id="a")
    make_credential(client, person_id=a, credential_type="OLD", issued_at="2025-01-01")
    make_project(client, name="P", region="华东", project_type="光伏",
                 started_at="2026-03-01", project_id="p1")
    make_position(client, project_id="p1", role="总监", holder_id="a")
    make_equivalence(client, source_type="OLD", target_type="NEW")
    draft = draft_rule(client, body={"requirements": {"总监": ["NEW"]}})
    run = client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    data = client.get(f"/dry-runs/{run['dry_run_id']}").json()
    assert data["results"][0]["outcome"] == "review"


def test_grandfathered_project_shows_rule_reason_in_run(client):
    alice = make_person(client, name="Alice", region="华东", person_id="alice")
    make_credential(client, person_id=alice, credential_type="C1", issued_at="2025-01-01")
    make_project(client, name="P", region="华东", project_type="光伏",
                 started_at="2026-03-01", project_id="p1")
    make_position(client, project_id="p1", role="安全员", holder_id="alice")
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}},
        effective_from="2026-01-01", created_by="m", approver="a",
    )
    draft = draft_rule(
        client, body={"requirements": {"安全员": ["C2"]}},
        supersedes_version_id=v1["version_id"], transition={"grandfather": True},
    )
    run = client.post("/dry-runs", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    row = client.get(f"/dry-runs/{run['dry_run_id']}").json()["results"][0]
    assert row["rule_version_id"] == v1["version_id"]
    assert "过渡条款保留旧资格" in row["rule_selection_reason"]
