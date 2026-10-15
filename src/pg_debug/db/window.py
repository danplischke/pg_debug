"""How long the server's counters have been counting.

Every finding argued from a counter is argued from what happened since the
counter last started from zero. An index unused for a year and one unused
since a crash an hour ago look the same in ``pg_stat_user_indexes``; only the
window tells them apart.

The window is not the server's uptime. A clean shutdown writes the counters
out and the next start reads them back, so ``pg_postmaster_start_time()``
moves while they keep counting. What starts them again is recorded:

- ``pg_stat_reset()`` sets ``stats_reset`` in ``pg_stat_database`` for the
  database it ran in;
- a crash, an immediate shutdown, a failover or a restore from a backup
  discards every counter, and the shared counters record when they started
  again: ``pg_stat_bgwriter.stats_reset``. The database's ``stats_reset``
  is then NULL, as for one never reset.

So the table and index counters of this database have been counting since
the later of the two. ``pg_stat_reset_shared('bgwriter')`` moves the second
without touching table counters, which makes the window read shorter than it
is: an error on the side of not judging.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import LiteralString
from typing import cast

import humanize

from .driver import SqlDriver
from .extensions import get_postgres_version
from .restricted import SafeSqlDriver
from .statements import StatementStatistics

TABLE_WINDOW_SQL = """
SELECT
    d.stats_reset AS database_reset,
    b.stats_reset AS server_reset,
    now() AS now
FROM pg_stat_database d
CROSS JOIN pg_stat_bgwriter b
WHERE d.datname = current_database()
"""

#: PostgreSQL 17 moved the checkpoint counters to pg_stat_checkpointer,
#: which records its own reset.
CHECKPOINTER_WINDOW_SQL = "SELECT stats_reset AS server_reset, now() AS now FROM pg_stat_checkpointer"
BGWRITER_WINDOW_SQL = "SELECT stats_reset AS server_reset, now() AS now FROM pg_stat_bgwriter"


@dataclass(frozen=True)
class CountingWindow:
    """When a set of counters last started from zero, and what started them.

    Attributes:
        since: When they started counting, or None when no start was
            recorded, which leaves the window unknown.
        now: The server's time when this was read.
        began_with: What started them: ``reset`` for ``pg_stat_reset()``
            or ``pg_stat_statements_reset()``, ``restart`` for a crash,
            immediate shutdown, failover or restore (or a reset of the
            shared counters), or None when unknown.
        evicted: For pg_stat_statements only: how many times it had to
            evict statements to make room for new ones since ``since``, so
            that some statements have counted for less than the window.
            None where it does not apply or the extension cannot say.
    """

    since: datetime | None
    now: datetime
    began_with: str | None = None
    evicted: int | None = None

    @property
    def seconds(self) -> float | None:
        return (self.now - self.since).total_seconds() if self.since is not None else None

    def at_least(self, days: float) -> bool:
        """Whether the counters have counted for at least this long; an unknown window has not."""
        seconds = self.seconds
        return seconds is not None and seconds >= days * 86400

    def describe(self) -> str:
        """``since 2026-10-14 21:01 UTC (3 hours)``, or that it is not known."""
        if self.since is None:
            return "since a time PostgreSQL did not record"
        when = self.since.strftime("%Y-%m-%d %H:%M %Z").strip()
        return f"since {when} ({humanize.naturaldelta(self.now - self.since)})"

    def to_dict(self) -> dict[str, Any]:
        """The window as evidence: when it began, how long it is in seconds, and what began it."""
        evidence: dict[str, Any] = {
            "counting_since": self.since.isoformat() if self.since else None,
            "counting_seconds": round(self.seconds) if self.seconds is not None else None,
            "counting_began_with": self.began_with,
        }
        if self.evicted is not None:
            evidence["statements_evicted"] = self.evicted
        return evidence


async def table_window(sql_driver: SqlDriver) -> CountingWindow:
    """How long this database's table and index counters have been counting."""
    rows = await SafeSqlDriver.execute_param_query(sql_driver, TABLE_WINDOW_SQL)
    cells = rows[0].cells if rows else {}
    return database_window(cells.get("database_reset"), cells.get("server_reset"), cells.get("now") or datetime.now().astimezone())


def database_window(database_reset: datetime | None, server_reset: datetime | None, now: datetime) -> CountingWindow:
    """The window of one database's counters in ``pg_stat_database``, from its ``stats_reset`` and the shared counters' reset."""
    if database_reset is not None and (server_reset is None or database_reset >= server_reset):
        return CountingWindow(since=database_reset, now=now, began_with="reset")
    if server_reset is not None:
        return CountingWindow(since=server_reset, now=now, began_with="restart")
    return CountingWindow(since=None, now=now)


async def checkpoint_window(sql_driver: SqlDriver) -> CountingWindow:
    """How long the checkpoint counters have been counting."""
    version = await get_postgres_version(sql_driver)
    rows = await SafeSqlDriver.execute_param_query(sql_driver, CHECKPOINTER_WINDOW_SQL if version >= 17 else BGWRITER_WINDOW_SQL)
    cells = rows[0].cells if rows else {}
    now = cells.get("now") or datetime.now().astimezone()
    since = cells.get("server_reset")
    return CountingWindow(since=since, now=now, began_with="restart" if since is not None else None)


async def statement_window(sql_driver: SqlDriver, stats: StatementStatistics) -> CountingWindow:
    """How long pg_stat_statements has been counting, and how often it evicted entries since.

    Only from pg_stat_statements 1.9 (PostgreSQL 14), which records its own
    reset in ``pg_stat_statements_info``; before that the window is unknown.
    It is not the table counters' window: ``pg_stat_statements_reset()`` and
    ``pg_stat_reset()`` are separate, and a crash loses the statements
    recorded since the last clean shutdown without moving its ``stats_reset``.
    """
    if not stats.has_info:
        rows = await SafeSqlDriver.execute_param_query(sql_driver, "SELECT now() AS now")
        return CountingWindow(since=None, now=rows[0].cells["now"] if rows else datetime.now().astimezone())
    schema = stats.relation.rsplit(".", 1)[0]
    rows = await SafeSqlDriver.execute_param_query(
        sql_driver,
        cast(LiteralString, f"SELECT stats_reset, dealloc, now() AS now FROM {schema}.pg_stat_statements_info"),
    )
    cells = rows[0].cells if rows else {}
    since = cells.get("stats_reset")
    return CountingWindow(
        since=since,
        now=cells.get("now") or datetime.now().astimezone(),
        began_with="reset" if since is not None else None,
        evicted=cells.get("dealloc"),
    )
