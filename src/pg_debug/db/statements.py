"""Where pg_stat_statements is, which columns it has, and which of its rows are this database's workload."""

from __future__ import annotations

from dataclasses import dataclass

from .driver import SqlDriver
from .restricted import SafeSqlDriver

PG_STAT_STATEMENTS = "pg_stat_statements"

#: The extension's schema and version, in one read. Its version, not the
#: server's, decides its columns: a cluster upgraded without ``ALTER EXTENSION
#: pg_stat_statements UPDATE`` runs the new library under the old view.
LOCATE_SQL = """
SELECT quote_ident(n.nspname) AS schema, e.extversion AS version
FROM pg_extension e
JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname = {}
"""

#: A row of pg_stat_statements counts statements in every database on the
#: server, and those of another database name tables this one does not have.
THIS_DATABASE = "dbid = (SELECT oid FROM pg_database WHERE datname = current_database())"


def _version(text: str) -> tuple[int, ...]:
    """``'1.10'`` as ``(1, 10)``, so it compares as a version and not as text."""
    parts: list[int] = []
    for part in text.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


@dataclass(frozen=True)
class StatementStatistics:
    """pg_stat_statements as this server has it.

    Attributes:
        relation: The view, schema-qualified. It is installed wherever the
            user put it (``extensions`` on Supabase), and pg-debug's own
            statements run with the search path the user chose, which need
            not include it.
        version: The extension's version, such as ``(1, 10)``.
    """

    relation: str
    version: tuple[int, ...]

    @property
    def total_time(self) -> str:
        # 1.8, which came with PostgreSQL 13, split planning from execution
        # and renamed the execution columns.
        return "total_exec_time" if self.version >= (1, 8) else "total_time"

    @property
    def mean_time(self) -> str:
        return "mean_exec_time" if self.version >= (1, 8) else "mean_time"

    @property
    def stddev_time(self) -> str:
        return "stddev_exec_time" if self.version >= (1, 8) else "stddev_time"

    @property
    def has_wal(self) -> bool:
        return self.version >= (1, 8)

    @property
    def has_toplevel(self) -> bool:
        """1.9, with PostgreSQL 14, keeps a statement run inside a function
        apart from the same statement run at top level, as two rows with the
        same queryid, user and database."""
        return self.version >= (1, 9)


async def locate_pg_stat_statements(sql_driver: SqlDriver) -> StatementStatistics | None:
    """Find pg_stat_statements in this database, or None when it is not installed here."""
    rows = await SafeSqlDriver.execute_param_query(sql_driver, LOCATE_SQL, [PG_STAT_STATEMENTS])
    if not rows:
        return None
    cells = rows[0].cells
    return StatementStatistics(relation=f"{cells['schema']}.{PG_STAT_STATEMENTS}", version=_version(str(cells["version"])))
