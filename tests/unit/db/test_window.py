"""How long the counters have been counting: the later of a reset and a crash, never the uptime."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.statements import StatementStatistics
from pg_debug.db.window import BGWRITER_WINDOW_SQL
from pg_debug.db.window import CHECKPOINTER_WINDOW_SQL
from pg_debug.db.window import TABLE_WINDOW_SQL
from pg_debug.db.window import CountingWindow
from pg_debug.db.window import checkpoint_window
from pg_debug.db.window import statement_window
from pg_debug.db.window import table_window

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
DAY = timedelta(days=1)


def answering(**cells) -> MagicMock:
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"now": NOW, **cells})])
    return driver


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_reset", "server_reset", "since", "began_with"),
    [
        # pg_stat_reset() in this database, after the server last discarded its counters.
        (NOW - DAY, NOW - 30 * DAY, NOW - DAY, "reset"),
        # A crash since: it discards the database's counters and leaves its stats_reset NULL.
        (None, NOW - 2 * DAY, NOW - 2 * DAY, "restart"),
        # A crash after an older reset.
        (NOW - 30 * DAY, NOW - 2 * DAY, NOW - 2 * DAY, "restart"),
        # Nothing recorded at all.
        (None, None, None, None),
    ],
    ids=["reset", "crash", "crash-after-reset", "unrecorded"],
)
async def test_the_table_counters_count_since_the_later_of_a_reset_and_a_crash(database_reset, server_reset, since, began_with):
    window = await table_window(answering(database_reset=database_reset, server_reset=server_reset))

    assert (window.since, window.began_with) == (since, began_with)


def test_the_window_is_not_the_uptime():
    """A clean restart writes the counters out and reads them back."""
    assert "pg_postmaster_start_time" not in TABLE_WINDOW_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "sql"), [(17, CHECKPOINTER_WINDOW_SQL), (16, BGWRITER_WINDOW_SQL)])
async def test_the_checkpoint_counters_window_is_read_where_the_version_keeps_them(version, sql):
    driver = answering(server_reset=NOW - DAY)

    with patch("pg_debug.db.window.get_postgres_version", AsyncMock(return_value=version)):
        window = await checkpoint_window(driver)

    assert driver.execute_query.await_args.args[0] == sql
    assert window.since == NOW - DAY


@pytest.mark.asyncio
async def test_pg_stat_statements_says_when_it_was_reset_and_how_often_it_evicted():
    driver = answering(stats_reset=NOW - 3 * DAY, dealloc=7)

    window = await statement_window(driver, StatementStatistics("extensions.pg_stat_statements", (1, 10)))

    assert "FROM extensions.pg_stat_statements_info" in driver.execute_query.await_args.args[0]
    assert (window.since, window.evicted, window.began_with) == (NOW - 3 * DAY, 7, "reset")


@pytest.mark.asyncio
async def test_pg_stat_statements_before_1_9_cannot_say_when_it_was_reset():
    window = await statement_window(answering(), StatementStatistics("public.pg_stat_statements", (1, 8)))

    assert window.since is None
    assert window.describe() == "since a time PostgreSQL did not record"


def test_a_window_reads_as_evidence():
    window = CountingWindow(since=NOW - DAY, now=NOW, began_with="restart", evicted=0)

    assert window.to_dict() == {
        "counting_since": "2026-10-13T12:00:00+00:00",
        "counting_seconds": 86400,
        "counting_began_with": "restart",
        "statements_evicted": 0,
    }
    assert window.describe() == "since 2026-10-13 12:00 UTC (a day)"
    assert window.at_least(1) and not window.at_least(1.5)


def test_an_unknown_window_is_never_long_enough():
    assert not CountingWindow(since=None, now=NOW).at_least(0.001)


def test_the_window_reads_run_in_restricted_mode():
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    for sql in (
        TABLE_WINDOW_SQL,
        CHECKPOINTER_WINDOW_SQL,
        BGWRITER_WINDOW_SQL,
        "SELECT stats_reset, dealloc, now() AS now FROM public.pg_stat_statements_info",
    ):
        safe._validate(sql)
