"""测试数据构造工厂（通过 HTTP 接口造数）。"""


def make_person(client, *, name, region, person_id=None):
    payload = {"name": name, "region": region}
    if person_id:
        payload["id"] = person_id
    response = client.post("/admin/persons", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_credential(client, *, person_id, credential_type, issued_at, credential_id=None):
    payload = {"person_id": person_id, "credential_type": credential_type, "issued_at": issued_at}
    if credential_id:
        payload["id"] = credential_id
    response = client.post("/admin/credentials", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_project(client, *, name, region, project_type, started_at, project_id=None,
                 status="in_progress", owner_contact=None, attributes=None):
    payload = {
        "name": name, "region": region, "project_type": project_type,
        "started_at": started_at, "status": status,
    }
    if project_id:
        payload["id"] = project_id
    if owner_contact:
        payload["owner_contact"] = owner_contact
    if attributes is not None:
        payload["attributes"] = attributes
    response = client.post("/admin/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_position(client, *, project_id, role, holder_id=None, required=None,
                  position_id=None, position_order=None):
    payload = {"project_id": project_id, "role": role, "holder_id": holder_id,
               "required_credential_types": required or []}
    if position_id:
        payload["id"] = position_id
    if position_order is not None:
        payload["position_order"] = position_order
    response = client.post("/admin/positions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_equivalence(client, *, source_type, target_type):
    response = client.post("/admin/equivalences", json={
        "source_type": source_type, "target_type": target_type,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def publish_rule(client, *, region, project_type, body, effective_from, created_by,
                 approver, effective_to=None, supersedes_version_id=None, transition=None):
    """创建草案 -> 非维护人批准 -> 原子发布，返回各阶段 ID。"""
    draft_payload = {
        "region": region, "project_type": project_type, "body": body,
        "effective_from": effective_from, "created_by": created_by,
    }
    if effective_to:
        draft_payload["effective_to"] = effective_to
    if supersedes_version_id:
        draft_payload["supersedes_version_id"] = supersedes_version_id
    if transition is not None:
        draft_payload["transition"] = transition
    drafted = client.post("/rules/drafts", json=draft_payload)
    assert drafted.status_code == 201, drafted.text
    version_id = drafted.json()["id"]
    approved = client.post(f"/rules/{version_id}/approve", json={"approver": approver})
    assert approved.status_code == 201, approved.text
    published = client.post(f"/rules/{version_id}/publish", json={"actor": approver})
    assert published.status_code == 201, published.text
    return {"version_id": version_id, **published.json()}


def draft_rule(client, **overrides):
    payload = {
        "region": "华东", "project_type": "光伏",
        "body": {"requirements": {}, "auto_equivalences": [], "exemptions": []},
        "effective_from": "2026-10-01", "created_by": "maintainer-a",
    }
    payload.update(overrides)
    response = client.post("/rules/drafts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]
