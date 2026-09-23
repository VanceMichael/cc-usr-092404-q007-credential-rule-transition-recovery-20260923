"""建立凭证、项目、规则时间线、演练与待发箱表。"""

from alembic import op

from skill_engine.models import metadata

revision = "002_impact_dryrun"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
