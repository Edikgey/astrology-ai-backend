"""change session_token to UUID

Revision ID: dd2ec94c54ed
Revises: 728b491a3ea4
Create Date: 2025-04-06 19:02:19.476444

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dd2ec94c54ed'
down_revision: Union[str, None] = '728b491a3ea4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.execute("""
        ALTER TABLE natal_charts
        ALTER COLUMN session_token
        SET DATA TYPE UUID USING session_token::uuid
    """)

def downgrade():
    op.execute("""
        ALTER TABLE natal_charts
        ALTER COLUMN session_token
        SET DATA TYPE VARCHAR
    """)