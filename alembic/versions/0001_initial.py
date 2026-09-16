"""Initial authoritative platform schema.

The detailed schema is generated from the version-pinned SQLAlchemy metadata. This
revision is intentionally the only place that provisions it; application startup
never calls metadata.create_all(). Subsequent changes use explicit Alembic diffs.
"""

from alembic import op
from platform_service.infrastructure.database import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in Base.metadata.sorted_tables:
        table.create(bind, checkfirst=False)


def downgrade():
    bind = op.get_bind()
    for table in reversed(Base.metadata.sorted_tables):
        table.drop(bind, checkfirst=False)
