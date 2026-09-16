"""${message}"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
def upgrade() -> None:
    ${upgrades if upgrades else "pass"}
def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
