"""Litestar 应用装配。"""

from litestar import Litestar, Request, get, post
from litestar.response import Response
from sqlalchemy import text

from . import catalog, dryrun, evaluation, rules
from .database import create_database_engine
from .errors import BadRequest, DomainError


def _domain_handler(_request: Request, exc: DomainError) -> Response:
    return Response(
        status_code=exc.status_code,
        content={"error": exc.__class__.__name__, "message": exc.message},
    )


async def _body(request: Request, required: tuple[str, ...] = ()) -> dict:
    data = await request.json()
    missing = [key for key in required if key not in data]
    if missing:
        raise BadRequest(f"缺少必填字段: {', '.join(missing)}")
    return data


def create_app(engine=None) -> Litestar:
    storage = engine or create_database_engine()

    # ---- 基础目录 ----------------------------------------------------------

    @post("/admin/persons")
    async def create_person(request: Request) -> dict:
        data = await _body(request, ("name", "region"))
        with storage.begin() as conn:
            pid = catalog.upsert_person(
                conn, person_id=data.get("id"), name=data["name"], region=data["region"]
            )
        return {"id": pid}

    @post("/admin/credentials")
    async def create_credential(request: Request) -> dict:
        data = await _body(request, ("person_id", "credential_type", "issued_at"))
        with storage.begin() as conn:
            cid = catalog.issue_credential(
                conn,
                person_id=data["person_id"],
                credential_type=data["credential_type"],
                issued_at=data["issued_at"],
                credential_id=data.get("id"),
            )
        return {"id": cid}

    @post("/admin/credentials/{credential_id:str}/revoke")
    async def revoke_credential(credential_id: str, request: Request) -> dict:
        data = await _body(request, ("revoked_at",))
        with storage.begin() as conn:
            catalog.revoke_credential(
                conn, credential_id=credential_id, revoked_at=data["revoked_at"]
            )
        return {"id": credential_id, "status": "revoked"}

    @post("/admin/projects")
    async def create_project(request: Request) -> dict:
        data = await _body(request, ("name", "region", "project_type", "started_at"))
        with storage.begin() as conn:
            pid = catalog.upsert_project(
                conn,
                project_id=data.get("id"),
                name=data["name"],
                region=data["region"],
                project_type=data["project_type"],
                started_at=data["started_at"],
                status=data.get("status", "in_progress"),
                owner_contact=data.get("owner_contact"),
                attributes=data.get("attributes"),
            )
        return {"id": pid}

    @post("/admin/projects/{project_id:str}/sign")
    async def sign_project(project_id: str, request: Request) -> dict:
        data = await _body(request, ("signed_at",))
        with storage.begin() as conn:
            return catalog.sign_project(
                conn, project_id=project_id, signed_at=data["signed_at"]
            )

    @post("/admin/positions")
    async def create_position(request: Request) -> dict:
        data = await _body(request, ("project_id", "role"))
        with storage.begin() as conn:
            pos_id = catalog.assign_position(
                conn,
                project_id=data["project_id"],
                role=data["role"],
                holder_id=data.get("holder_id"),
                required_credential_types=data.get("required_credential_types"),
                position_id=data.get("id"),
                position_order=data.get("position_order"),
            )
        return {"id": pos_id}

    @post("/admin/equivalences")
    async def create_equivalence(request: Request) -> dict:
        data = await _body(request, ("source_type", "target_type"))
        with storage.begin() as conn:
            eid = catalog.register_manual_equivalence(
                conn,
                source_type=data["source_type"],
                target_type=data["target_type"],
            )
        return {"id": eid, "status": "pending"}

    # ---- 规则草案 / 批准 / 时间线 ------------------------------------------

    @post("/rules/drafts")
    async def create_draft(request: Request) -> dict:
        data = await _body(request, ("region", "project_type", "body", "effective_from", "created_by"))
        with storage.begin() as conn:
            result = rules.create_draft(
                conn,
                region=data["region"],
                project_type=data["project_type"],
                body=data["body"],
                effective_from=data["effective_from"],
                effective_to=data.get("effective_to"),
                created_by=data["created_by"],
                supersedes_version_id=data.get("supersedes_version_id"),
                transition=data.get("transition"),
            )
        return result

    @post("/rules/{version_id:str}/approve")
    async def approve_rule(version_id: str, request: Request) -> dict:
        data = await _body(request, ("approver",))
        with storage.begin() as conn:
            rules.approve(conn, version_id=version_id, approver=data["approver"])
        return {"id": version_id, "status": "approved"}

    @post("/rules/{version_id:str}/publish")
    async def publish_rule(version_id: str, request: Request) -> dict:
        data = await _body(request)
        with storage.begin() as conn:
            result = rules.publish(conn, version_id=version_id, actor=data.get("actor", "publisher"))
        return result

    @post("/rules/{version_id:str}/withdraw")
    async def withdraw_rule(version_id: str, request: Request) -> dict:
        data = await _body(request, ("actor",))
        with storage.begin() as conn:
            return rules.withdraw(
                conn,
                version_id=version_id,
                actor=data["actor"],
                effective_from=data.get("effective_from"),
            )

    @post("/rules/{version_id:str}/extend")
    async def extend_rule(version_id: str, request: Request) -> dict:
        data = await _body(request, ("actor", "new_effective_to"))
        with storage.begin() as conn:
            return rules.extend(
                conn,
                version_id=version_id,
                actor=data["actor"],
                new_effective_to=data["new_effective_to"],
            )

    @get("/rules/timeline")
    async def get_timeline(region: str, project_type: str) -> dict:
        with storage.connect() as conn:
            events = rules.timeline(conn, region=region, project_type=project_type)
        return {"region": region, "project_type": project_type, "events": events}

    # ---- 单项目裁定 / 演练 / 待发箱 ----------------------------------------

    @post("/projects/{project_id:str}/evaluate")
    async def evaluate_project(project_id: str, request: Request) -> dict:
        data = await _body(request, ("draft_version_id", "as_of"))
        with storage.connect() as conn:
            return evaluation.evaluate_project(
                conn,
                project_id=project_id,
                candidate_version_id=data["draft_version_id"],
                as_of=data["as_of"],
            )

    @post("/dry-runs")
    async def start_dry_run(request: Request) -> dict:
        data = await _body(request, ("draft_version_id", "as_of"))
        with storage.begin() as conn:
            return dryrun.start_dry_run(
                conn,
                draft_version_id=data["draft_version_id"],
                as_of=data["as_of"],
                project_ids=data.get("project_ids"),
                batch_size=int(data.get("batch_size", 0)),
            )

    @post("/dry-runs/{dry_run_id:str}/resume")
    async def resume_dry_run(dry_run_id: str, request: Request) -> dict:
        data = await _body(request)
        with storage.begin() as conn:
            return dryrun.resume_dry_run(
                conn, dry_run_id=dry_run_id, batch_size=int(data.get("batch_size", 0))
            )

    @get("/dry-runs/{dry_run_id:str}")
    async def get_dry_run(dry_run_id: str) -> dict:
        with storage.connect() as conn:
            return dryrun.get_dry_run(conn, dry_run_id)

    @get("/notifications")
    async def list_notifications(limit: int = 100) -> dict:
        with storage.connect() as conn:
            return {"pending": dryrun.pending_notifications(conn, limit=limit)}

    @post("/notifications/{notification_id:int}/deliver")
    async def deliver_notification(notification_id: int) -> dict:
        with storage.begin() as conn:
            dryrun.mark_delivered(conn, notification_id)
        return {"id": notification_id, "status": "sent"}

    @get("/health", sync_to_thread=False)
    def health() -> dict[str, str]:
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "storage": "sqlite"}

    return Litestar(
        route_handlers=[
            create_person,
            create_credential,
            revoke_credential,
            create_project,
            sign_project,
            create_position,
            create_equivalence,
            create_draft,
            approve_rule,
            publish_rule,
            withdraw_rule,
            extend_rule,
            get_timeline,
            evaluate_project,
            start_dry_run,
            resume_dry_run,
            get_dry_run,
            list_notifications,
            deliver_notification,
            health,
        ],
        exception_handlers={DomainError: _domain_handler},
    )


app = create_app()
