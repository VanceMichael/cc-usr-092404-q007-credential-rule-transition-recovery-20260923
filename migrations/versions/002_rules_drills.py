"""资格规则换版与影响演练领域表。"""

from alembic import op
import sqlalchemy as sa

revision = "002_rules_drills"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("is_rule_maintainer", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "persons",
        sa.Column("person_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
    )
    op.create_table(
        "credentials",
        sa.Column("credential_id", sa.String(), primary_key=True),
        sa.Column("person_id", sa.String(), sa.ForeignKey("persons.person_id"), nullable=False),
        sa.Column("credential_code", sa.String(), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("revoked_on", sa.Date(), nullable=True),
    )
    op.create_table(
        "credential_equivalences",
        sa.Column("equivalence_id", sa.String(), primary_key=True),
        sa.Column("required_code", sa.String(), nullable=False),
        sa.Column("accepted_code", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.UniqueConstraint("required_code", "accepted_code", name="uq_equiv_pair"),
    )
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("project_type", sa.String(), nullable=False),
        sa.Column("manager_recipient", sa.String(), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("completed_on", sa.Date(), nullable=True),
        sa.Column("exemption_code", sa.String(), nullable=True),
        sa.Column("exemption_expires_on", sa.Date(), nullable=True),
    )
    op.create_table(
        "project_positions",
        sa.Column("position_id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("person_id", sa.String(), sa.ForeignKey("persons.person_id"), nullable=False),
        sa.Column("signed_on", sa.Date(), nullable=True),
        sa.Column("signed_rule_version", sa.String(), nullable=True),
        sa.Column("signed_match", sa.Boolean(), nullable=True),
    )
    op.create_table(
        "rule_drafts",
        sa.Column("draft_id", sa.String(), primary_key=True),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("project_type", sa.String(), nullable=False),
        sa.Column("new_version", sa.String(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("end_on", sa.Date(), nullable=True),
        sa.Column("supersedes_version", sa.String(), nullable=False),
        sa.Column("personnel_requirements", sa.JSON(), nullable=False),
        sa.Column("grandfather_policy", sa.JSON(), nullable=False),
        sa.Column("change_note", sa.String(), nullable=False, server_default=""),
        sa.Column("maintainer", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("draft_id", "draft_version", name="uq_draft_version"),
    )
    op.create_table(
        "published_rules",
        sa.Column("published_id", sa.String(), primary_key=True),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("project_type", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("start_on", sa.Date(), nullable=False),
        sa.Column("end_on", sa.Date(), nullable=True),
        sa.Column("supersedes_version", sa.String(), nullable=True),
        sa.Column("personnel_requirements", sa.JSON(), nullable=False),
        sa.Column("grandfather_policy", sa.JSON(), nullable=False),
        sa.Column("source_draft_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("region", "project_type", "version", name="uq_published_scope_version"),
    )
    op.create_table(
        "rule_timeline_heads",
        sa.Column("head_key", sa.String(), primary_key=True),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("project_type", sa.String(), nullable=False),
        sa.Column(
            "current_published_id",
            sa.String(),
            sa.ForeignKey("published_rules.published_id"),
            nullable=True,
        ),
        sa.Column("tail_end_on", sa.Date(), nullable=True),
    )
    op.create_table(
        "rule_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("head_key", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("published_id", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("head_key", "seq", name="uq_event_seq"),
    )
    op.create_table(
        "drill_runs",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("draft_id", sa.String(), sa.ForeignKey("rule_drafts.draft_id"), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("watermark_date", sa.Date(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("scope_region", sa.String(), nullable=True),
        sa.Column("scope_project_type", sa.String(), nullable=True),
        sa.Column("input_digest", sa.String(), nullable=False, unique=True),
        sa.Column("frozen_inputs", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("total_projects", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_projects", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified_projects", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "drill_project_results",
        sa.Column("result_id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("drill_runs.run_id"), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("fragment_digest", sa.String(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("run_id", "project_id", name="uq_run_project"),
    )
    op.create_table(
        "subscriptions",
        sa.Column("subscription_id", sa.String(), primary_key=True),
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("project_type", sa.String(), nullable=False),
        sa.UniqueConstraint("recipient", "region", "project_type", name="uq_subscription"),
    )
    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.String(), primary_key=True),
        sa.Column("idempotency_key", sa.String(), nullable=False, unique=True),
        sa.Column("recipient", sa.String(), nullable=False, index=True),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("subscriptions")
    op.drop_table("drill_project_results")
    op.drop_table("drill_runs")
    op.drop_table("rule_events")
    op.drop_table("rule_timeline_heads")
    op.drop_table("published_rules")
    op.drop_table("rule_drafts")
    op.drop_table("project_positions")
    op.drop_table("projects")
    op.drop_table("credential_equivalences")
    op.drop_table("credentials")
    op.drop_table("persons")
    op.drop_table("users")
