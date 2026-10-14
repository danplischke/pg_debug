import logging
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from psycopg.conninfo import make_conninfo

import pg_debug.top_queries
from pg_debug import PgDebug
from pg_debug.db import QUERY_MARKER
from pg_debug.db import SqlDriver
from pg_debug.errors import ExtensionRequiredError
from pg_debug.top_queries import TopQueriesCalc

logger = logging.getLogger(__name__)

COUNT_QUERY = "SELECT COUNT(*) FROM test_items"
FILTER_QUERY = """
    SELECT name, value
    FROM test_items
    WHERE value > 500
    ORDER BY value DESC
"""
CROSS_JOIN_QUERY = """
    SELECT t1.name, t2.name
    FROM test_items t1
    CROSS JOIN test_items t2
    WHERE t1.value > t2.value
    LIMIT 100
"""


@pytest.fixture
def connection_string(test_postgres_connection_string) -> str:
    connection_string, version = test_postgres_connection_string
    logger.info("Using connection string: %s", connection_string)
    logger.info("Using version: %s", version)
    return connection_string


@pytest_asyncio.fixture
async def local_sql_driver(connection_string):
    """Create a SQL driver connected to a real PostgreSQL database."""
    driver = SqlDriver(engine_url=connection_string)
    driver.connect()  # This is not an async method, no await needed
    try:
        yield driver
    finally:
        # Cleanup
        if hasattr(driver, "conn") and driver.conn is not None:
            await driver.conn.close()


async def setup_test_data(sql_driver):
    """Create the table the workload runs against, and start statistics from zero."""
    # Ensure pg_stat_statements extension is available
    rows = await sql_driver.execute_query("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements'")
    if not rows:
        pytest.skip("pg_stat_statements extension not available")
    try:
        await sql_driver.execute_query("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        logger.info("pg_stat_statements extension created or already exists")
    except Exception as e:
        logger.warning("Unable to create pg_stat_statements extension: %s", e)
        pytest.skip("pg_stat_statements extension not available or cannot be created")

    await sql_driver.execute_query("""
        DROP TABLE IF EXISTS test_items;
        CREATE TABLE test_items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            value INTEGER NOT NULL
        )
    """)
    await sql_driver.execute_query("""
        INSERT INTO test_items (name, value)
        SELECT
            'Item ' || i,
            (random() * 1000)::INTEGER
        FROM generate_series(1, 1000) i
    """)

    # Reset pg_stat_statements so only the workload below is recorded.
    await sql_driver.execute_query("SELECT pg_stat_statements_reset()")


async def run_workload(connection_string: str):
    """Run the queries an application would, over a plain connection.

    Everything issued through ``SqlDriver`` carries the ``/* pg_debug */``
    marker and is deliberately left out of the top-queries report, so the
    workload the report is meant to find has to come from somewhere else —
    exactly as it does in production.
    """
    async with await psycopg.AsyncConnection.connect(connection_string, autocommit=True) as app:
        for _ in range(10):
            await app.execute(COUNT_QUERY)
        for _ in range(5):
            await app.execute(FILTER_QUERY)
        for _ in range(10):
            await app.execute(CROSS_JOIN_QUERY)


async def cleanup_test_data(sql_driver):
    """Clean up test data."""
    try:
        await sql_driver.execute_query("DROP TABLE IF EXISTS test_items")
        await sql_driver.execute_query("SELECT pg_stat_statements_reset()")
    except Exception as e:
        logger.warning("Error cleaning up test data: %s", e)


def _sort_column(row: dict[str, Any], prefix: str) -> str:
    """The name of the time column, which pg_stat_statements renamed in 13."""
    matches = [key for key in row if key.startswith(prefix)]
    assert len(matches) == 1, f"expected one {prefix}* column, got {sorted(row)}"
    return matches[0]


@pytest.mark.asyncio
async def test_get_top_queries_integration(local_sql_driver, connection_string):
    """A workload run outside pg_debug is reported; pg_debug's own queries are not."""
    try:
        await setup_test_data(local_sql_driver)
        await run_workload(connection_string)

        # Verify pg_stat_statements has captured our queries
        pg_stats = await local_sql_driver.execute_query("SELECT query FROM pg_stat_statements WHERE query LIKE '%CROSS JOIN%' LIMIT 1")
        if not pg_stats:
            pytest.skip("pg_stat_statements did not capture the CROSS JOIN query")

        calc = TopQueriesCalc(sql_driver=local_sql_driver)

        total_result = await calc.get_top_queries_by_time(limit=10, sort_by="total")
        mean_result = await calc.get_top_queries_by_time(limit=10, sort_by="mean")

        logger.info("Top queries by total time: %s", total_result)
        logger.info("Top queries by mean time: %s", mean_result)

        assert total_result, "no queries reported"
        for row in total_result:
            assert {"query", "calls", "rows"} <= set(row)
            assert QUERY_MARKER not in row["query"], f"pg_debug's own query reported as workload: {row['query']!r}"

        by_text = {row["query"]: row for row in total_result}
        count_row = next((row for text, row in by_text.items() if "COUNT(*)" in text), None)
        cross_join_row = next((row for text, row in by_text.items() if "CROSS JOIN" in text), None)
        assert count_row is not None and cross_join_row is not None, f"workload missing from {sorted(by_text)}"
        assert count_row["calls"] == 10
        assert cross_join_row["calls"] == 10

        total_column = _sort_column(total_result[0], "total_")
        totals = [row[total_column] for row in total_result]
        assert totals == sorted(totals, reverse=True), "not ordered by total execution time"

        mean_column = _sort_column(mean_result[0], "mean_")
        means = [row[mean_column] for row in mean_result]
        assert means == sorted(means, reverse=True), "not ordered by mean execution time"
    finally:
        await cleanup_test_data(local_sql_driver)


@pytest.mark.asyncio
async def test_extension_not_available(local_sql_driver):
    """Without pg_stat_statements the report refuses, and says how to install it."""
    calc = TopQueriesCalc(sql_driver=local_sql_driver)

    with pytest.MonkeyPatch().context() as mp:

        async def not_installed(*args, **kwargs):
            return None

        # Patch the name TopQueriesCalc actually calls, not the one it was imported from.
        mp.setattr(pg_debug.top_queries, "locate_pg_stat_statements", not_installed)

        with pytest.raises(ExtensionRequiredError) as excinfo:
            await calc.get_top_queries_by_time()

    message = str(excinfo.value)
    assert "not currently installed" in message
    assert "CREATE EXTENSION" in message


OTHER_DATABASE = "pgdebug_top_queries_other"


@pytest.mark.asyncio
async def test_the_workload_is_this_database_s_read_from_where_the_extension_is(connection_string):
    """Supabase installs pg_stat_statements in the schema extensions. With a
    search path that left its schema out, every reader failed with "relation
    pg_stat_statements does not exist"; and every reader counted the
    statements of every database on the server as this one's workload."""
    with psycopg.connect(connection_string, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements'").fetchone():
            pytest.skip("pg_stat_statements extension not available")
        admin.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
        admin.execute("CREATE SCHEMA IF NOT EXISTS extensions")
        admin.execute("CREATE SCHEMA IF NOT EXISTS reporting")
        admin.execute("CREATE EXTENSION pg_stat_statements SCHEMA extensions")
        admin.execute(f"DROP DATABASE IF EXISTS {OTHER_DATABASE}")
        admin.execute(f"CREATE DATABASE {OTHER_DATABASE}")
    other = make_conninfo(connection_string, dbname=OTHER_DATABASE)
    try:
        with psycopg.connect(other, autocommit=True) as elsewhere:
            for _ in range(5):
                elsewhere.execute("SELECT 1 AS only_in_the_other_database")
        with psycopg.connect(connection_string, autocommit=True) as here:
            for _ in range(5):
                here.execute("SELECT 1 AS only_in_this_database")

        async with PgDebug(connection_string, search_path="reporting") as pg:
            ranked = await pg.get_top_queries(sort_by="total_time", limit=500)
            resources = await pg.get_top_queries(sort_by="resources")

        texts = [row["query"] for row in ranked]
        # Constants are recorded as $1; the aliases are kept.
        assert any("only_in_this_database" in text for text in texts), texts
        assert not any("only_in_the_other_database" in text for text in texts + [row["query"] for row in resources])
    finally:
        with psycopg.connect(connection_string, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {OTHER_DATABASE}")
            # Back where the other tests expect it.
            admin.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
            admin.execute("CREATE EXTENSION pg_stat_statements SCHEMA public")
            admin.execute("DROP SCHEMA IF EXISTS extensions")
