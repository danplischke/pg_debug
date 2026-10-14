"""Whether a table has statistics, read where a reset of the counters cannot reach."""

import time

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.index.base import ANALYZED_SQL
from pg_debug.statistics import StatisticsInspector

ANALYZED = "pgdebug_stats_analyzed"
NEVER = "pgdebug_stats_never"


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


@pytest.fixture
def reset_counters(url):
    """One table analyzed, one with data and never analyzed, and the activity
    counters reset afterwards, as a crash or pg_stat_reset() leaves them."""
    with psycopg.connect(url, autocommit=True) as conn:
        for table in (ANALYZED, NEVER):
            # No autovacuum, which would analyze the second table behind the test's back.
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"CREATE TABLE {table} (id int) WITH (autovacuum_enabled = false)")
            conn.execute(f"INSERT INTO {table} SELECT generate_series(1, 5000)")
        conn.execute(f"ANALYZE {ANALYZED}")
    with psycopg.connect(url, autocommit=True) as conn:
        # A backend sends its counts on its own schedule; reset only once they
        # have arrived, or they arrive after the reset and undo it.
        deadline = time.monotonic() + 10
        while conn.execute(
            "SELECT count(*) FROM pg_stat_user_tables WHERE relname = ANY(%s) AND n_tup_ins = 5000", [[ANALYZED, NEVER]]
        ).fetchone() != (2,):
            assert time.monotonic() < deadline, "the INSERTs never reached the statistics"
            time.sleep(0.1)
            conn.execute("SELECT pg_stat_clear_snapshot()")
        conn.execute("SELECT pg_stat_reset()")
    try:
        yield
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            for table in (ANALYZED, NEVER):
                conn.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_counters")
async def test_a_reset_does_not_hide_a_table_without_statistics(url):
    """Judged by the counters, every table looked current after a reset, and
    one never analyzed was not reported, for want of a row count."""
    async with PgDebug(url) as pg:
        stale = {row["table"]: row for row in await StatisticsInspector(await pg.sql_driver()).stale_tables()}

    assert stale[NEVER]["never_analyzed"] is True
    assert stale[NEVER]["size_bytes"] > 0
    assert ANALYZED not in stale


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_counters")
async def test_a_reset_does_not_make_the_tuning_advisor_refuse(url):
    """It read last_analyze alone, which a reset clears and autovacuum never sets."""
    async with PgDebug(url) as pg:
        rows = await (await pg.sql_driver()).execute_query(ANALYZED_SQL)

    assert rows and rows[0].cells["analyzed"] is True
