"""领域表结构（SQLAlchemy Core）。

生产库由 Alembic 迁移建表；测试中可直接 ``metadata.create_all``。
两者必须保持一致，迁移 002 是这里的 DDL 镜像。
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

metadata = MetaData()

# 平台用户（规则维护者 / 批准人 / 操作人）
users = Table(
    "users",
    metadata,
    Column("user_id", String, primary_key=True),
    Column("display_name", String, nullable=False),
    Column("is_rule_maintainer", Boolean, nullable=False, server_default=text("0")),
)

# 人员及其所属地区（替补只在同地区内挑选）
persons = Table(
    "persons",
    metadata,
    Column("person_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("region", String, nullable=False),
)

# 能力凭证（带签发/撤销日期，支持固定水位回放）
credentials = Table(
    "credentials",
    metadata,
    Column("credential_id", String, primary_key=True),
    Column("person_id", String, ForeignKey("persons.person_id"), nullable=False),
    Column("credential_code", String, nullable=False),
    Column("issued_on", Date, nullable=False),
    Column("revoked_on", Date, nullable=True),
)

# 等效凭证裁定：accepted 自动接受，pending 进入人工复核
credential_equivalences = Table(
    "credential_equivalences",
    metadata,
    Column("equivalence_id", String, primary_key=True),
    Column("required_code", String, nullable=False),
    Column("accepted_code", String, nullable=False),
    Column("status", String, nullable=False),  # accepted | pending
    UniqueConstraint("required_code", "accepted_code", name="uq_equiv_pair"),
)

# 在办/已完成项目
projects = Table(
    "projects",
    metadata,
    Column("project_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    Column("manager_recipient", String, nullable=False),
    Column("started_on", Date, nullable=False),
    Column("completed_on", Date, nullable=True),
    Column("exemption_code", String, nullable=True),
    Column("exemption_expires_on", Date, nullable=True),
)

# 项目岗位：signed_* 非空表示该岗位的核验结论已签署（换版不重开）
project_positions = Table(
    "project_positions",
    metadata,
    Column("position_id", String, primary_key=True),
    Column("project_id", String, ForeignKey("projects.project_id"), nullable=False),
    Column("role", String, nullable=False),
    Column("person_id", String, ForeignKey("persons.person_id"), nullable=False),
    Column("signed_on", Date, nullable=True),
    Column("signed_rule_version", String, nullable=True),
    Column("signed_match", Boolean, nullable=True),
)

# 规则草案：声明地区、项目类型、有效区间、替代关系与过渡条款
rule_drafts = Table(
    "rule_drafts",
    metadata,
    Column("draft_id", String, primary_key=True),
    Column("draft_version", Integer, nullable=False),
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    Column("new_version", String, nullable=False),
    Column("effective_on", Date, nullable=False),
    Column("end_on", Date, nullable=True),  # NULL 表示开放区间
    Column("supersedes_version", String, nullable=False),
    Column("personnel_requirements", JSON, nullable=False),
    Column("grandfather_policy", JSON, nullable=False),
    Column("change_note", String, nullable=False, server_default=text("''")),
    Column("maintainer", String, nullable=False),
    Column("status", String, nullable=False),  # draft | pending_approval | approved | active | superseded | withdrawn
    Column("content_hash", String, nullable=False),
    Column("approved_by", String, nullable=True),
    Column("approved_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("draft_id", "draft_version", name="uq_draft_version"),
)

# 正式发布的规则版本（时间线上的每一段）
published_rules = Table(
    "published_rules",
    metadata,
    Column("published_id", String, primary_key=True),
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    Column("version", String, nullable=False),
    Column("start_on", Date, nullable=False),
    Column("end_on", Date, nullable=True),  # 被截断/延期时就地更新
    Column("supersedes_version", String, nullable=True),
    Column("personnel_requirements", JSON, nullable=False),
    Column("grandfather_policy", JSON, nullable=False),
    Column("source_draft_id", String, nullable=True),
    Column("status", String, nullable=False),  # current | superseded | withdrawn
    Column("published_at", DateTime, nullable=False),
    UniqueConstraint("region", "project_type", "version", name="uq_published_scope_version"),
)

# 每个适用范围（地区+项目类型）唯一的时间线游标
rule_timeline_heads = Table(
    "rule_timeline_heads",
    metadata,
    Column("head_key", String, primary_key=True),  # f"{region}|{project_type}"
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    Column("current_published_id", String, ForeignKey("published_rules.published_id"), nullable=True),
    Column("tail_end_on", Date, nullable=True),  # 当前段为空时，时间线收尾日
)

# 时间线事件账本（append-only，(head_key, seq) 唯一）
rule_events = Table(
    "rule_events",
    metadata,
    Column("event_id", String, primary_key=True),
    Column("head_key", String, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("event_type", String, nullable=False),  # bootstrapped | activated | withdrew | extended
    Column("published_id", String, nullable=True),
    Column("version", String, nullable=True),
    Column("actor", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("occurred_at", DateTime, nullable=False),
    UniqueConstraint("head_key", "seq", name="uq_event_seq"),
)

# 演练运行：固定凭证水位 + 输入摘要
drill_runs = Table(
    "drill_runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("draft_id", String, ForeignKey("rule_drafts.draft_id"), nullable=False),
    Column("draft_version", Integer, nullable=False),
    Column("watermark_date", Date, nullable=False),
    Column("effective_on", Date, nullable=False),
    Column("scope_region", String, nullable=True),
    Column("scope_project_type", String, nullable=True),
    Column("input_digest", String, nullable=False, unique=True),
    Column("frozen_inputs", JSON, nullable=True),  # 启动时固化的水位快照，续跑不重读实时表
    Column("status", String, nullable=False),  # running | completed
    Column("total_projects", Integer, nullable=False, server_default=text("0")),
    Column("completed_projects", Integer, nullable=False, server_default=text("0")),
    Column("notified_projects", Integer, nullable=False, server_default=text("0")),
    Column("created_at", DateTime, nullable=False),
    Column("completed_at", DateTime, nullable=True),
)

# 逐项目演练结果（断点检查点）
drill_project_results = Table(
    "drill_project_results",
    metadata,
    Column("result_id", String, primary_key=True),
    Column("run_id", String, ForeignKey("drill_runs.run_id"), nullable=False),
    Column("project_id", String, nullable=False),
    Column("status", String, nullable=False),  # pending | completed
    Column("outcome", String, nullable=True),  # compliant | staffing | review | exempt | completed_snapshot
    Column("fragment_digest", String, nullable=True),
    Column("detail", JSON, nullable=True),
    Column("notified", Boolean, nullable=False, server_default=text("0")),
    UniqueConstraint("run_id", "project_id", name="uq_run_project"),
)

# 通知订阅（region/project_type 用 '*' 表示全域）
subscriptions = Table(
    "subscriptions",
    metadata,
    Column("subscription_id", String, primary_key=True),
    Column("recipient", String, nullable=False),
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    UniqueConstraint("recipient", "region", "project_type", name="uq_subscription"),
)

# 持久待发箱
notifications = Table(
    "notifications",
    metadata,
    Column("notification_id", String, primary_key=True),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("recipient", String, nullable=False, index=True),
    Column("topic", String, nullable=False),  # drill_decision | rule_timeline
    Column("subject", String, nullable=False),
    Column("body", Text, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String, nullable=False, server_default="pending"),  # pending | sending | sent
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error", String, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("sent_at", DateTime, nullable=True),
)
