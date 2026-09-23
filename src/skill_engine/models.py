"""SQLAlchemy 表定义（Core 元数据）。"""

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

persons = Table(
    "persons",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("region", String, nullable=False),
    Column("created_at", String, nullable=False),
)

credentials = Table(
    "credentials",
    metadata,
    Column("id", String, primary_key=True),
    Column("person_id", String, nullable=False),
    Column("credential_type", String, nullable=False),
    Column("issued_at", String, nullable=False),
    # revoke 时间为空表示当前仍有效；水位评估按 issued_at/revoked_at 截取快照。
    Column("revoked_at", String, nullable=True),
)

projects = Table(
    "projects",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    # in_progress（在办）/ signed（已签署完成）。
    Column("status", String, nullable=False),
    Column("started_at", String, nullable=False),
    Column("signed_at", String, nullable=True),
    # 签署时固定的规则版本快照；签署项目不再受后续换版影响。
    Column("signed_rule_version_id", String, nullable=True),
    Column("owner_contact", String, nullable=True),
    Column("attributes", Text, nullable=False, default="{}"),
)

positions = Table(
    "positions",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("role", String, nullable=False),
    Column("position_order", Integer, nullable=False, default=0),
    Column("holder_id", String, nullable=True),
    # 项目自身声明的岗位要求（现行引擎按此匹配），规则未覆盖该角色时沿用。
    Column("required_credential_types", Text, nullable=False, default="[]"),
)

# 人工等效项：pending 表示无法自动裁定，演练中必须转入复核。
equivalences = Table(
    "equivalences",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_type", String, nullable=False),
    Column("target_type", String, nullable=False),
    Column("mode", String, nullable=False),  # manual
    Column("status", String, nullable=False),  # pending / approved / rejected
    Column("created_at", String, nullable=False),
)

# 规则版本：草案与生效版本同表，用 status 区分。
rule_versions = Table(
    "rule_versions",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope_key", String, nullable=False),
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    Column("status", String, nullable=False),  # draft/approved/published/withdrawn
    Column("version_seq", Integer, nullable=True),
    Column("content_hash", String, nullable=False),
    # body: {requirements: {role: [凭证类型...]}, auto_equivalences: [[a,b]...],
    #        exemptions: [{role, when: {属性: 值}}]}
    Column("body", Text, nullable=False),
    Column("effective_from", String, nullable=False),
    Column("effective_to", String, nullable=True),
    Column("supersedes_version_id", String, nullable=True),
    # transition: {grandfather: true/false}，生效前已启动项目是否保留旧资格。
    Column("transition", Text, nullable=False, default="{}"),
    Column("created_by", String, nullable=False),
    Column("approved_by", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("approved_at", String, nullable=True),
    Column("published_at", String, nullable=True),
    UniqueConstraint("scope_key", "version_seq", name="uq_rule_scope_seq"),
)

# 每个（地区, 项目类型）范围一条指针行，CAS 版本号保证时间线唯一。
rule_scopes = Table(
    "rule_scopes",
    metadata,
    Column("scope_key", String, primary_key=True),
    Column("region", String, nullable=False),
    Column("project_type", String, nullable=False),
    Column("active_version_id", String, nullable=True),
    Column("current_seq", Integer, nullable=False, default=0),
)

rule_timeline_events = Table(
    "rule_timeline_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope_key", String, nullable=False),
    Column("seq", Integer, nullable=False),
    # publish / withdraw / extend。
    Column("event_type", String, nullable=False),
    Column("rule_version_id", String, nullable=False),
    Column("effective_from", String, nullable=False),
    Column("effective_to", String, nullable=True),
    Column("actor", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("note", String, nullable=True),
    UniqueConstraint("scope_key", "seq", name="uq_timeline_scope_seq"),
)

dry_runs = Table(
    "dry_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("draft_version_id", String, nullable=False),
    Column("as_of", String, nullable=False),
    # 全部输入（草案内容、水位、基础数据指纹）的摘要；相同摘要直接复用。
    Column("input_summary", String, nullable=False),
    Column("status", String, nullable=False),  # running / completed
    Column("total_projects", Integer, nullable=False, default=0),
    Column("processed_projects", Integer, nullable=False, default=0),
    # 启动时固化的目标项目 ID 列表，长批次按序处理，重启后据此续跑。
    Column("project_ids", Text, nullable=False, default="[]"),
    Column("reused_from_id", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("completed_at", String, nullable=True),
)

dry_run_results = Table(
    "dry_run_results",
    metadata,
    Column("id", String, primary_key=True),
    Column("dry_run_id", String, nullable=False),
    Column("project_id", String, nullable=False),
    # 该项目相关输入切片的哈希；与缓存一致即复用，不重算。
    Column("input_key", String, nullable=False),
    Column("outcome", String, nullable=False),
    Column("rule_version_id", String, nullable=True),
    Column("rule_selection_reason", String, nullable=False),
    Column("detail", Text, nullable=False),
    # 1=命中内容缓存未重算，0=本次实际计算；用于核验“局部修改只重算相关项目”。
    Column("from_cache", Integer, nullable=False, default=0),
    UniqueConstraint("dry_run_id", "project_id", name="uq_dryrun_project"),
)

# 内容寻址的裁定缓存，跨演练复用：相同输入切片必然得到相同结果。
decision_cache = Table(
    "decision_cache",
    metadata,
    Column("input_key", String, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

dry_run_checkpoints = Table(
    "dry_run_checkpoints",
    metadata,
    Column("dry_run_id", String, primary_key=True),
    Column("last_order", Integer, nullable=False, default=0),
)

# 持久待发箱：同事务落库，idempotency_key 防同一决定重复通知。
outbox = Table(
    "outbox",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("recipient", String, nullable=False),
    Column("event_type", String, nullable=False),
    Column("subject", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String, nullable=False, default="pending"),  # pending / sent
    Column("created_at", String, nullable=False),
    Column("delivered_at", String, nullable=True),
)
