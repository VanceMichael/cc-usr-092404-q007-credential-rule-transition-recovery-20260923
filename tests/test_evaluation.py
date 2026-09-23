"""固定凭证水位上的单项目裁定。"""

from factories import (
    draft_rule,
    make_credential,
    make_equivalence,
    make_person,
    make_position,
    make_project,
    publish_rule,
)


def _new_rule_body_v2():
    return {
        "requirements": {"安全总监": ["ESG-ADV"], "现场审核": ["ESG-AUDIT"]},
        "auto_equivalences": [["ESG-AUDIT", "ESG-AUDIT-V2"]],
        "exemptions": [{"role": "现场审核", "when": {"scale": "小微"}}],
    }


def _setup_world(client):
    # 在办项目 p1：安全员持旧证 C1，新规要求 C2 → 补员，并有替补。
    alice = make_person(client, name="Alice", region="华东", person_id="alice")
    bob = make_person(client, name="Bob", region="华东", person_id="bob")
    carol = make_person(client, name="Carol", region="华北", person_id="carol")
    make_credential(client, person_id=alice, credential_type="C1", issued_at="2025-01-01")
    make_credential(client, person_id=bob, credential_type="C2", issued_at="2025-06-01")
    make_credential(client, person_id=carol, credential_type="C2", issued_at="2025-06-01")

    p1 = make_project(
        client, name="屋顶光伏A", region="华东", project_type="光伏",
        started_at="2026-03-01", project_id="p1", owner_contact="alice@green",
    )
    make_position(client, project_id=p1, role="安全员", holder_id="alice",
                  required=["C1"], position_id="p1-pos")
    return {"alice": alice, "bob": bob, "p1": p1}


def test_project_backfill_lists_substitutes_and_explains_new_rule(client):
    world = _setup_world(client)
    draft = draft_rule(
        client,
        body={"requirements": {"安全员": ["C2"]}, "auto_equivalences": [], "exemptions": []},
        transition={"grandfather": False},
    )
    response = client.post(f"/projects/{world['p1']}/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    })
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["outcome"] == "backfill"
    assert data["rule_version_id"] == draft
    assert "采用新规则" in data["rule_selection_reason"]
    pos = data["positions"][0]
    assert pos["status"] == "backfill"
    assert pos["reasons"], "必须给出进入补员的原因"
    # 同地区的 Bob 可用；跨地区 Carol 不可用；不含在岗者本人。
    substitutes = {s["person_id"] for s in pos["substitutes"]}
    assert substitutes == {"bob"}


def test_grandfather_keeps_old_qualification_under_transition(client):
    world = _setup_world(client)
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}, "auto_equivalences": [], "exemptions": []},
        effective_from="2026-01-01", created_by="m", approver="a",
    )
    draft = draft_rule(
        client, body={"requirements": {"安全员": ["C2"]}, "auto_equivalences": [], "exemptions": []},
        supersedes_version_id=v1["version_id"], transition={"grandfather": True},
    )
    response = client.post(f"/projects/{world['p1']}/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    })
    data = response.json()
    assert data["rule_selection_kind"] == "grandfathered"
    assert data["rule_version_id"] == v1["version_id"]
    assert "过渡条款保留旧资格" in data["rule_selection_reason"]
    assert data["outcome"] == "compliant"


def test_signed_project_is_pinned_to_snapshot(client):
    world = _setup_world(client)
    v1 = publish_rule(
        client, region="华东", project_type="光伏",
        body={"requirements": {"安全员": ["C1"]}, "auto_equivalences": [], "exemptions": []},
        effective_from="2026-01-01", created_by="m", approver="a",
    )
    # 签署固定 v1 快照。
    signed = client.post(f"/admin/projects/{world['p1']}/sign", json={"signed_at": "2026-09-01"})
    assert signed.status_code == 201
    draft = draft_rule(
        client, body={"requirements": {"安全员": ["C2"]}, "auto_equivalences": [], "exemptions": []},
        supersedes_version_id=v1["version_id"], transition={"grandfather": False},
    )
    data = client.post(f"/projects/{world['p1']}/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert data["rule_selection_kind"] == "signed_snapshot"
    assert data["rule_version_id"] == v1["version_id"]
    assert data["outcome"] == "compliant"
    assert "固定签署时规则快照" in data["rule_selection_reason"]


def test_exemption_when_attributes_match(client):
    make_person(client, name="A", region="华东", person_id="a")
    p = make_project(
        client, name="小微光伏", region="华东", project_type="光伏",
        started_at="2026-03-01", project_id="p", attributes={"scale": "小微"},
    )
    make_position(client, project_id=p, role="现场审核", holder_id="a")
    draft = draft_rule(client, body=_new_rule_body_v2())
    data = client.post(f"/projects/p/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert data["outcome"] == "exempt"
    assert "豁免" in data["positions"][0]["reasons"][0]


def test_manual_equivalence_goes_to_review(client):
    a = make_person(client, name="A", region="华东", person_id="a")
    make_credential(client, person_id=a, credential_type="ESG-OLD", issued_at="2025-01-01")
    p = make_project(
        client, name="复核项目", region="华东", project_type="光伏",
        started_at="2026-03-01", project_id="p",
    )
    make_position(client, project_id=p, role="安全总监", holder_id=a)
    make_equivalence(client, source_type="ESG-OLD", target_type="ESG-ADV")
    draft = draft_rule(client, body=_new_rule_body_v2())
    data = client.post(f"/projects/p/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert data["outcome"] == "review"
    pos = data["positions"][0]
    assert pos["manual_equivalences"], "必须列出无法自动裁定的等效项"
    assert "无法自动判定" in pos["reasons"][0]


def test_watermark_excludes_future_and_counts_revocation_risk(client):
    alice = make_person(client, name="Alice", region="华东", person_id="alice")
    c1 = make_credential(client, person_id=alice, credential_type="C2",
                         issued_at="2025-01-01", credential_id="c1")
    future = make_credential(client, person_id=alice, credential_type="C3",
                             issued_at="2026-12-01", credential_id="cfuture")
    p = make_project(
        client, name="水位项目", region="华东", project_type="光伏",
        started_at="2026-03-01", project_id="p",
    )
    make_position(client, project_id=p, role="安全员", holder_id="alice")
    draft = draft_rule(client, body={"requirements": {"安全员": ["C2"]}})

    before = client.post("/projects/p/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert before["outcome"] == "compliant"

    # 撤销在评估水位之前：失去资格并追加撤销风险。
    client.post(f"/admin/credentials/{c1}/revoke", json={"revoked_at": "2026-09-20"})
    after = client.post("/projects/p/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert after["outcome"] == "backfill"
    risk = after["positions"][0]["revocation_risks"]
    assert risk and risk[0]["credential_id"] == "c1"

    # 用更早的水位重评：撤销尚未发生，凭证仍有效。
    historical = client.post("/projects/p/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-19",
    }).json()
    assert historical["outcome"] == "compliant"
    assert not historical["positions"][0]["revocation_risks"]


def test_watermark_future_credential_not_counted(client):
    a = make_person(client, name="A", region="华东", person_id="a")
    make_credential(client, person_id=a, credential_type="C2", issued_at="2026-12-01")
    p = make_project(
        client, name="未来凭证项目", region="华东", project_type="光伏",
        started_at="2026-03-01", project_id="p",
    )
    make_position(client, project_id=p, role="安全员", holder_id=a)
    draft = draft_rule(client, body={"requirements": {"安全员": ["C2"]}})
    data = client.post("/projects/p/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert data["outcome"] == "backfill"


def test_auto_equivalence_satisfies_requirement(client):
    a = make_person(client, name="A", region="华东", person_id="a")
    make_credential(client, person_id=a, credential_type="ESG-AUDIT-V2", issued_at="2025-01-01")
    p = make_project(
        client, name="自动等效项目", region="华东", project_type="光伏",
        started_at="2026-03-01", project_id="p",
    )
    make_position(client, project_id=p, role="现场审核", holder_id=a)
    draft = draft_rule(client, body=_new_rule_body_v2())
    data = client.post(f"/projects/p/evaluate", json={
        "draft_version_id": draft, "as_of": "2026-09-23",
    }).json()
    assert data["outcome"] == "compliant"
    assert data["positions"][0]["matched_credentials"] == ["ESG-AUDIT-V2"]
