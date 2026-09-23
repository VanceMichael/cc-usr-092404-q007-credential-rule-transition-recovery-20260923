"""HTTP 装配：规则生命周期、影响演练、持久待发箱接口。"""

import logging
from datetime import date
from typing import Any

from litestar import Litestar, Request, get, post
from litestar.response import Response
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from . import schema
from .services import drills, outbox, rules
from .services.clock import Clock
from .services.errors import DomainError

logger = logging.getLogger("skill_engine.notifications")


def _noop_sender(item: dict[str, Any]) -> None:
    """默认投递器：生产环境替换为邮件/消息网关，按 idempotency_key 幂等。"""
    logger.info("deliver notification %s to %s: %s", item["idempotency_key"], item["recipient"], item["subject"])


def _parse_date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError(f"{field} 必须是 YYYY-MM-DD 日期") from None


def create_app(
    engine: Engine | None = None,
    *,
    clock: Clock | None = None,
    sender: outbox.Sender | None = None,
) -> Litestar:
    from .database import create_database_engine

    storage = engine or create_database_engine()
    app_clock = clock or Clock()
    app_sender = sender or _noop_sender

    # ---------------------------------------------------------------- 健康

    @get("/health")
    def health() -> dict[str, str]:
        from sqlalchemy import text

        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "storage": "sqlite"}

    # ---------------------------------------------------------------- 演示数据

    @post("/admin/seed")
    def admin_seed(data: dict[str, Any]) -> dict[str, Any]:
        """幂等写入演示/测试基线（按主键覆盖），可选引导规则时间线。"""
        counts: dict[str, int] = {}
        with storage.begin() as conn:
            table_specs = [
                ("users", schema.users),
                ("persons", schema.persons),
                ("credentials", schema.credentials),
                ("equivalences", schema.credential_equivalences),
                ("projects", schema.projects),
                ("positions", schema.project_positions),
                ("subscriptions", schema.subscriptions),
            ]
            for key, table in table_specs:
                rows = data.get(key, [])
                pk = table.primary_key.columns.values()[0].name
                for raw in rows:
                    row = dict(raw)
                    for date_field in (
                        "issued_on",
                        "revoked_on",
                        "started_on",
                        "completed_on",
                        "exemption_expires_on",
                        "signed_on",
                    ):
                        if date_field in row:
                            row[date_field] = _parse_date(row[date_field], date_field)
                    conn.execute(delete(table).where(table.c[pk] == row[pk]))
                    conn.execute(table.insert().values(**row))
                counts[key] = len(rows)
        for tl in data.get("timelines", []):
            try:
                rules.bootstrap_timeline(
                    storage,
                    clock=app_clock,
                    region=tl["region"],
                    project_type=tl["project_type"],
                    version=tl["version"],
                    start_on=_parse_date(tl["start_on"], "start_on"),  # type: ignore[arg-type]
                    personnel_requirements=tl["personnel_requirements"],
                    grandfather_policy=tl.get("grandfather_policy"),
                )
                counts.setdefault("timelines", 0)
                counts["timelines"] += 1
            except DomainError as exc:
                if "时间线已存在" not in exc.message:
                    raise
        return {"seeded": counts}

    # ---------------------------------------------------------------- 订阅

    @post("/subscriptions")
    def add_subscription(data: dict[str, Any]) -> dict[str, Any]:
        import uuid

        with storage.begin() as conn:
            existing = conn.execute(
                select(schema.subscriptions.c.subscription_id).where(
                    schema.subscriptions.c.recipient == data["recipient"],
                    schema.subscriptions.c.region == data["region"],
                    schema.subscriptions.c.project_type == data["project_type"],
                )
            ).first()
            if existing is None:
                conn.execute(
                    schema.subscriptions.insert().values(
                        subscription_id=uuid.uuid4().hex,
                        recipient=data["recipient"],
                        region=data["region"],
                        project_type=data["project_type"],
                    )
                )
        return {"status": "ok"}

    @get("/subscriptions")
    def list_subscriptions() -> list[dict[str, Any]]:
        with storage.connect() as conn:
            rows = conn.execute(select(schema.subscriptions)).mappings().all()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 规则草案

    @post("/rule-drafts")
    def create_rule_draft(data: dict[str, Any]) -> dict[str, Any]:
        return rules.create_draft(
            storage,
            clock=app_clock,
            maintainer=data["maintainer"],
            content=data["content"],
        )

    @get("/rule-drafts")
    def list_rule_drafts() -> list[dict[str, Any]]:
        return rules.list_drafts(storage)

    @get("/rule-drafts/{draft_id:str}")
    def get_rule_draft(draft_id: str) -> dict[str, Any]:
        return rules.get_draft(storage, draft_id)

    @post("/rule-drafts/{draft_id:str}/revisions")
    def revise_rule_draft(draft_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return rules.update_draft(
            storage,
            clock=app_clock,
            maintainer=data["maintainer"],
            draft_id=draft_id,
            content=data["content"],
        )

    @post("/rule-drafts/{draft_id:str}/submit")
    def submit_rule_draft(draft_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return rules.submit_for_approval(
            storage, clock=app_clock, maintainer=data["maintainer"], draft_id=draft_id
        )

    @post("/rule-drafts/{draft_id:str}/approve")
    def approve_rule_draft(draft_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return rules.approve_draft(
            storage, clock=app_clock, approver=data["approver"], draft_id=draft_id
        )

    @post("/rule-drafts/{draft_id:str}/publish")
    def publish_rule_draft(draft_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return rules.publish_draft(
            storage, clock=app_clock, actor=data.get("actor", data.get("approver", "")), draft_id=draft_id
        )

    # ---------------------------------------------------------------- 时间线

    @post("/timelines/bootstrap")
    def bootstrap(data: dict[str, Any]) -> dict[str, Any]:
        return rules.bootstrap_timeline(
            storage,
            clock=app_clock,
            region=data["region"],
            project_type=data["project_type"],
            version=data["version"],
            start_on=_parse_date(data["start_on"], "start_on"),  # type: ignore[arg-type]
            personnel_requirements=data["personnel_requirements"],
            grandfather_policy=data.get("grandfather_policy"),
        )

    @get("/timelines/{region:str}/{project_type:str}")
    def get_timeline(region: str, project_type: str) -> dict[str, Any]:
        return rules.get_timeline(storage, region, project_type)

    @post("/rules/withdraw")
    def withdraw_rule(data: dict[str, Any]) -> dict[str, Any]:
        return rules.withdraw_rule(
            storage,
            clock=app_clock,
            actor=data["actor"],
            region=data["region"],
            project_type=data["project_type"],
            withdraw_on=_parse_date(data["withdraw_on"], "withdraw_on"),  # type: ignore[arg-type]
            reason=data.get("reason", ""),
        )

    @post("/rules/emergency-extend")
    def emergency_extend(data: dict[str, Any]) -> dict[str, Any]:
        return rules.emergency_extend(
            storage,
            clock=app_clock,
            actor=data["actor"],
            region=data["region"],
            project_type=data["project_type"],
            new_end_on=_parse_date(data["new_end_on"], "new_end_on"),  # type: ignore[arg-type]
            reason=data.get("reason", ""),
        )

    # ---------------------------------------------------------------- 演练

    @post("/drills")
    def start_drill(data: dict[str, Any]) -> dict[str, Any]:
        return drills.start_drill(
            storage,
            clock=app_clock,
            draft_id=data["draft_id"],
            watermark_date=_parse_date(data.get("watermark_date"), "watermark_date"),  # type: ignore[arg-type]
            scope_region=data.get("scope_region"),
            scope_project_type=data.get("scope_project_type"),
            batch_size=int(data.get("batch_size", 100)),
        )

    @post("/drills/{run_id:str}/resume")
    def resume_drill(run_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        limit = int((data or {}).get("limit", 100))
        return drills.resume_drill(storage, clock=app_clock, run_id=run_id, limit=limit)

    @get("/drills")
    def list_drills(draft_id: str | None = None) -> list[dict[str, Any]]:
        return drills.list_runs(storage, draft_id=draft_id)

    @get("/drills/{run_id:str}")
    def get_drill(run_id: str) -> dict[str, Any]:
        return drills.get_run(storage, run_id)

    @get("/drills/{run_id:str}/impact")
    def get_drill_impact(run_id: str) -> dict[str, Any]:
        return drills.get_impact(storage, run_id)

    @get("/drills/{run_id:str}/projects/{project_id:str}")
    def get_drill_project(run_id: str, project_id: str) -> dict[str, Any]:
        return drills.get_project_result(storage, run_id, project_id)

    # ---------------------------------------------------------------- 待发箱

    @get("/notifications")
    def list_notifications(status: str | None = None, recipient: str | None = None) -> list[dict[str, Any]]:
        with storage.connect() as conn:
            stmt = select(schema.notifications).order_by(schema.notifications.c.created_at)
            if status:
                stmt = stmt.where(schema.notifications.c.status == status)
            if recipient:
                stmt = stmt.where(schema.notifications.c.recipient == recipient)
            rows = conn.execute(stmt).mappings().all()
        result = []
        for r in rows:
            item = dict(r)
            item["created_at"] = item["created_at"].isoformat() if item["created_at"] else None
            item["sent_at"] = item["sent_at"].isoformat() if item["sent_at"] else None
            result.append(item)
        return result

    @get("/notifications/stats")
    def notifications_stats() -> dict[str, int]:
        return outbox.stats(storage)

    @post("/notifications/drain")
    def drain_notifications(data: dict[str, Any] | None = None) -> dict[str, int]:
        limit = int((data or {}).get("limit", 100))
        return outbox.send_pending(storage, clock=app_clock, sender=app_sender, limit=limit)

    routes = [
        health,
        admin_seed,
        add_subscription,
        list_subscriptions,
        create_rule_draft,
        list_rule_drafts,
        get_rule_draft,
        revise_rule_draft,
        submit_rule_draft,
        approve_rule_draft,
        publish_rule_draft,
        bootstrap,
        get_timeline,
        withdraw_rule,
        emergency_extend,
        start_drill,
        resume_drill,
        list_drills,
        get_drill,
        get_drill_impact,
        get_drill_project,
        list_notifications,
        notifications_stats,
        drain_notifications,
    ]

    def domain_error_handler(request: Request, exc: DomainError) -> Response:
        return Response(
            {"error": exc.code, "message": exc.message},
            status_code=exc.status_code,
        )

    return Litestar(
        route_handlers=routes,
        exception_handlers={DomainError: domain_error_handler},
    )
