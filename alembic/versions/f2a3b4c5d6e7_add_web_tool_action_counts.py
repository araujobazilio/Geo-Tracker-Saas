"""add explicit web tool action counters to prompt_runs and usage_events

Revision ID: f2a3b4c5d6e7
Revises: e1a2b3c4d5e6
Create Date: 2026-09-06 23:50:00.000000

Phase 13.5.12G — Web Tool Evidence & Billing Split.

Background
----------
The legacy ``search_requests`` column conflated two distinct concepts for
OpenAI WEB_GROUNDED executions:

1. the TOTAL number of built-in ``web_search_call`` output items processed
   (this is what OpenAI's ``max_tool_calls`` bound applies to), and
2. the number of documented billable ``search`` actions (this is what the
   ``search_per_1000_usd`` tariff applies to).

OpenAI ``web_search_call`` items carry ``action.type`` in
{``search``, ``open_page``, ``find_in_page``}.  Only ``search`` is
documented as incurring a web-search tool call cost.  Counting every item
as billable silently over-charged.

Schema changes (ADDITIVE ONLY)
------------------------------
Five nullable INTEGER columns are added to both ``prompt_runs`` and
``usage_events``:

- web_tool_call_count        (bound authority — total web_search_call items)
- search_action_count        (billing authority — action.type == search)
- open_page_action_count
- find_in_page_action_count
- unknown_web_action_count   (missing/unrecognized action — fail-closed)

Each column has a ``>= 0`` CHECK.  A sum-invariant CHECK enforces
``web_tool_call_count = search + open_page + find_in_page + unknown``
whenever ALL five columns are non-NULL.

Historical data policy
----------------------
- ``search_requests`` is NOT dropped, renamed, or rewritten.
- No backfill is performed.  Historical rows keep the new columns NULL,
  which the sum-invariant CHECK explicitly permits.
- Historical OpenAI rows have action breakdown = UNKNOWN; their exact
  search billing component must not be reinterpreted automatically.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("prompt_runs", "usage_events")

_COLUMNS: tuple[str, ...] = (
    "web_tool_call_count",
    "search_action_count",
    "open_page_action_count",
    "find_in_page_action_count",
    "unknown_web_action_count",
)

_SUM_INVARIANT_SQL = (
    "web_tool_call_count IS NULL OR search_action_count IS NULL "
    "OR open_page_action_count IS NULL OR find_in_page_action_count IS NULL "
    "OR unknown_web_action_count IS NULL "
    "OR web_tool_call_count = search_action_count + open_page_action_count "
    "+ find_in_page_action_count + unknown_web_action_count"
)


def _non_negative_name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_non_negative"


def _sum_name(table: str) -> str:
    return f"ck_{table}_web_tool_call_count_sum"


def upgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        for column in _COLUMNS:
            op.create_check_constraint(
                _non_negative_name(table, column),
                table,
                f"{column} IS NULL OR {column} >= 0",
            )
        op.create_check_constraint(_sum_name(table), table, _SUM_INVARIANT_SQL)


def downgrade() -> None:
    # Explicit, deterministic teardown: drop every constraint we created
    # BEFORE dropping the columns.  Do not rely on DROP COLUMN implicitly
    # removing constraints.
    for table in reversed(_TABLES):
        op.drop_constraint(_sum_name(table), table, type_="check")
        for column in reversed(_COLUMNS):
            op.drop_constraint(_non_negative_name(table, column), table, type_="check")
        for column in reversed(_COLUMNS):
            op.drop_column(table, column)
