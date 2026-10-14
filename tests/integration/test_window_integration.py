"""Counting windows against a real server: a reset starts the counters again, and the findings say so."""

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.health.checks.index import UNUSED_INDEXES

TABLE = "pgdebug_window_t"
INDEX = "pgdebug_window_t_value_idx"


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


@pytest.fixture
def reset_with_an_unscanned_index(url):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(f"CREATE TABLE {TABLE} (id int, value int)")
        conn.execute(f"CREATE INDEX {INDEX} ON {TABLE} (value)")
        conn.execute("SELECT pg_stat_reset()")
    try:
        yield
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_with_an_unscanned_index")
async def test_an_index_is_not_called_unused_right_after_a_reset(url):
    """Every index is unscanned the moment after pg_stat_reset(); calling
    them unused tells the reader to drop indexes the workload needs."""
    async with PgDebug(url) as pg:
        refused = (await pg.analyze_db_health("index"))[UNUSED_INDEXES]
        judged = (await pg.analyze_db_health("index", min_window_days=0))[UNUSED_INDEXES]
        windows = await pg.counting_windows()

    assert refused.healthy and refused.findings == []
    assert refused.metrics["judged"] is False
    assert refused.metrics["counting_began_with"] == "reset"
    assert refused.metrics["counting_seconds"] < 600
    assert "reset with pg_stat_reset()" in refused.summary

    assert judged.metrics["judged"] is True
    assert INDEX in [finding["index"] for finding in judged.findings]

    tables, checkpoints = windows["tables"], windows["checkpoints"]
    assert tables is not None and tables["counting_began_with"] == "reset"
    assert checkpoints is not None and checkpoints["counting_since"] is not None
