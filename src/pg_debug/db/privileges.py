"""What the statistics views show the connecting role, and what they withhold from it.

PostgreSQL shows a role the activity and the statements of other roles only
when it holds ``pg_read_all_stats``, which ``pg_monitor`` includes and a
superuser has the privileges of. Without it the rows are still there, with
their details withheld, and nothing in the result said so: ``activity`` read
"(nothing running)" while other roles' queries ran. The readers of those views
now say how much was withheld, and ask the server once, per command, whether
the role lacks it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .driver import SqlDriver
from .extensions import get_postgres_version
from .restricted import SafeSqlDriver

#: What ``pg_stat_activity`` and ``pg_stat_statements`` show in place of the
#: query text of a backend or a statement the reading role may not see.
HIDDEN_QUERY_TEXT = "<insufficient privilege>"

#: Why the details are withheld, said the same way by every reader.
MISSING_PRIVILEGE = "lacks pg_read_all_stats, which pg_monitor includes"

#: From PostgreSQL 15 the statistics views ask whether the role has the
#: privileges of pg_read_all_stats, which a member that does not inherit them
#: lacks; before, whether it is a member at all.
PRIVILEGES_OF_ROLE_MIN_VERSION = 15


@dataclass(frozen=True)
class StatisticsAccess:
    """Whether the connecting role is shown every role's activity and statements.

    Attributes:
        role: The role the statistics views were read as, ``current_user``.
        reads_all_stats: Whether it holds ``pg_read_all_stats`` the way the
            server's version asks, as a superuser always does.
    """

    role: str
    reads_all_stats: bool


async def statistics_access(sql_driver: SqlDriver | SafeSqlDriver) -> StatisticsAccess:
    """Ask the server whether the connecting role may see other roles' statistics.

    The question is the one the server asks before it shows a row: ``USAGE``
    from PostgreSQL 15, ``MEMBER`` before. Both are true for a superuser.
    """
    privilege = "USAGE" if await get_postgres_version(sql_driver) >= PRIVILEGES_OF_ROLE_MIN_VERSION else "MEMBER"
    rows = await sql_driver.execute_query(
        f"SELECT current_user AS role, pg_has_role(current_user, 'pg_read_all_stats', '{privilege}') AS reads_all_stats"
    )
    cells = rows[0].cells if rows else {}
    return StatisticsAccess(role=str(cells.get("role") or ""), reads_all_stats=bool(cells.get("reads_all_stats")))


class StatisticsRows(list[dict[str, Any]]):
    """Rows read from a statistics view, and what PostgreSQL withheld from the role that read them.

    It is a list, so code that iterates, indexes or compares the rows works as
    before. ``hidden`` says in a sentence what the role was not shown, how
    much, and why; it is None when nothing was withheld. ``hidden_count`` is
    how many backends or statements that is. Neither survives slicing or
    ``list()``, which make a plain list.
    """

    hidden: str | None
    hidden_count: int

    def __init__(self, rows: Any = (), hidden: str | None = None, hidden_count: int = 0):
        super().__init__(rows)
        self.hidden = hidden
        self.hidden_count = hidden_count


def withheld_rows(rows: list[dict[str, Any]]) -> int:
    """How many of the rows had their query text withheld."""
    return sum(1 for row in rows if row.get("query") == HIDDEN_QUERY_TEXT)


__all__ = [
    "HIDDEN_QUERY_TEXT",
    "MISSING_PRIVILEGE",
    "StatisticsAccess",
    "StatisticsRows",
    "statistics_access",
    "withheld_rows",
]
