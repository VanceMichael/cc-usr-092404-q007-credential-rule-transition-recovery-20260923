"""HTTP 端到端：从登记世界到补员清单、时间线变更、待发箱投递。"""

from datetime import date

from tests.conftest import (
    EFFECTIVE,
    NEW_VERSION,
    WATERMARK,
)


SEED = {
    "users": [
        {"user_id": "u-maintainer", "display_name": "维护者", "is_rule_maintainer": True},
        {"user_id": "u-approver", "display_name": "批准人", "is_rule_maintainer": False},
    ],
    "persons": [
        {"person_id": "p1", "name": "甲", "region": "华东"},
        {"person_id": "p2", "name": "乙", "region": "华东"},
    ],
    "credentials": [
        {"credential_id": "c1", "person_id": "p1", "credential_code": "ESG-A",
         "issued_on": "2024-01-01", "revoked_on": None},
        {"credential_id": "c2", "person_id": "p2", "credential_code": "ESG-B",
         "issued_on": "2024-01-01", "revoked_on": None},
        {"credential_id": "c3", "person_id": "p2", "credential_code": "ESG-A",
         "issued_on": "2024-01-01", "revoked_on": None},
    ],
    "projects": [
        {"project_id": "prj-1", "name": "在办项目", "region": "华东", "project_type": "光伏",
         "manager_recipient": "m1@example.com", "started_on": "2026-01-01",
         "completed_on": None, "exemption_code": None, "exemption_expires_on": None},
    ],
    "positions": [
        {"position_id": "pos1", "project_id": "prj-1", "role": "合规官", "person_id": "p1",
         "signed_on": None, "signed_rule_version": None, "signed_match": None},
    ],
    "subscriptions": [
        {"subscription_id": "s1", "recipient": "boss@example.com",
         "region": "华东", "project_type": "*"},
    ],
    "timelines": [
        {"region": "华东", "project_type": "光伏", "version": "v1", "start_on": "2024-01-01",
         "personnel_requirements": [{"role": "合规官", "required_credentials": ["ESG-A"]}]},
    ],
}

DRAFT_CONTENT = {
    "region": "华东",
    "project_type": "光伏",
    "new_version": NEW_VERSION,
    "effective_on": EFFECTIVE.isoformat(),
    "end_on": None,
    "supersedes_version": "v1",
    "personnel_requirements": [{"role": "合规官", "required_credentials": ["ESG-A", "ESG-B"]}],
    "grandfather_policy": {"mode": "project_start", "grace_days": 0},
    "change_note": "加严",
}


def test_full_workflow_over_http(client):
    r = client.post("/admin/seed", json=SEED)
    assert r.status_code == 201

    r = client.post("/rule-drafts", json={"maintainer": "u-maintainer", "content": DRAFT_CONTENT})
    assert r.status_code == 201
    draft_id = r.json()["draft_id"]

    # 非维护者不能提交
    r = client.post(f"/rule-drafts/{draft_id}/submit", json={"maintainer": "u-approver"})
    assert r.status_code == 403
    r = client.post(f"/rule-drafts/{draft_id}/submit", json={"maintainer": "u-maintainer"})
    assert r.status_code == 201

    # 维护者不能自批
    r = client.post(f"/rule-drafts/{draft_id}/approve", json={"approver": "u-maintainer"})
    assert r.status_code == 403
    r = client.post(f"/rule-drafts/{draft_id}/approve", json={"approver": "u-approver"})
    assert r.status_code == 201

    # 生效前演练
    r = client.post("/drills", json={
        "draft_id": draft_id, "watermark_date": WATERMARK.isoformat(),
    })
    assert r.status_code == 201
    run = r.json()
    assert run["status"] == "completed"
    assert run["outcome_counts"]["staffing"] == 1

    run_id = run["run_id"]
    r = client.get(f"/drills/{run_id}/impact")
    assert r.status_code == 200
    impact = r.json()
    positions = impact["affected_positions"]
    assert len(positions) == 1
    assert positions[0]["missing_credentials"] == ["ESG-B"]
    assert [c["person_id"] for c in positions[0]["available_replacements"]] == ["p2"]

    r = client.get(f"/drills/{run_id}/projects/prj-1")
    assert r.status_code == 200
    detail = r.json()
    assert detail["applicable_version"] == NEW_VERSION
    assert detail["outcome"] == "staffing"
    assert any("ESG-B" in reason for reason in detail["reasons"])

    # 影响清单先于切换产出，发布后时间线原子切换
    r = client.post(f"/rule-drafts/{draft_id}/publish", json={"actor": "u-approver"})
    assert r.status_code == 201
    timeline = r.json()
    assert [s["version"] for s in timeline["segments"]] == ["v1", "v2"]

    # 变更通知已入待发箱；决定通知也在
    r = client.get("/notifications/stats")
    stats = r.json()
    assert stats["pending"] >= 2

    r = client.post("/notifications/drain", json={"limit": 100})
    assert r.status_code == 201
    assert r.json()["sent"] >= 2
    r = client.get("/notifications")
    topics = sorted(n["topic"] for n in r.json())
    assert "drill_decision" in topics and "rule_timeline" in topics

    # 再投递不重复
    before = client.get("/notifications/stats").json()["sent"]
    client.post("/notifications/drain", json={"limit": 100})
    assert client.get("/notifications/stats").json()["sent"] == before


def test_drill_endpoint_resumes_partial_batch(client):
    client.post("/admin/seed", json=SEED)
    draft_id = client.post(
        "/rule-drafts", json={"maintainer": "u-maintainer", "content": DRAFT_CONTENT}
    ).json()["draft_id"]
    client.post(f"/rule-drafts/{draft_id}/submit", json={"maintainer": "u-maintainer"})
    client.post(f"/rule-drafts/{draft_id}/approve", json={"approver": "u-approver"})

    r = client.post("/drills", json={
        "draft_id": draft_id, "watermark_date": WATERMARK.isoformat(), "batch_size": 0,
    })
    run = r.json()
    assert run["status"] == "running" and run["completed_projects"] == 0
    r = client.post(f"/drills/{run['run_id']}/resume", json={"limit": 10})
    assert r.json()["status"] == "completed"


def test_domain_errors_map_to_http_status(client):
    r = client.get("/drills/no-such-run")
    assert r.status_code == 404
    assert r.json()["error"] == "NotFoundError"

    r = client.post("/rule-drafts", json={
        "maintainer": "u-maintainer",
        "content": {**DRAFT_CONTENT, "effective_on": "bad-date"},
    })
    assert r.status_code == 400


def test_timeline_endpoints(client):
    client.post("/admin/seed", json=SEED)
    r = client.get("/timelines/华东/光伏")
    assert r.status_code == 200
    assert r.json()["segments"][0]["version"] == "v1"

    r = client.post("/rules/withdraw", json={
        "actor": "u-approver", "region": "华东", "project_type": "光伏",
        "withdraw_on": "2026-12-01", "reason": "x",
    })
    assert r.status_code == 201
    assert r.json()["current_published_id"] is None

    r = client.post("/rules/emergency-extend", json={
        "actor": "u-approver", "region": "华东", "project_type": "光伏",
        "new_end_on": "2027-01-01", "reason": "y",
    })
    assert r.status_code == 201
    assert r.json()["tail_end_on"] == "2027-01-01"
