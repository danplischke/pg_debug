from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SqlDriver
from pg_debug.health import HealthType
from pg_debug.health.checks.statistics import StatisticsHealthCalc
from pg_debug.statistics import StatisticsInspector


def make_driver(rows=()):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=row) for row in rows])
    return driver


def executed_query(driver):
    return driver.execute_query.call_args[0][0]


# -- Column statistics ------------------------------------------------------


@pytest.mark.asyncio
async def test_column_stats_for_a_qualified_table():
    driver = make_driver([{"column": "user_id", "n_distinct": 500.0}])

    rows = await StatisticsInspector(driver).column_stats("reporting.orders")

    assert rows == [{"column": "user_id", "n_distinct": 500.0}]
    query = executed_query(driver)
    assert "'orders'" in query
    assert "'reporting'" in query


@pytest.mark.asyncio
async def test_column_stats_for_an_unqualified_table_uses_the_search_path():
    driver = make_driver()

    await StatisticsInspector(driver).column_stats("orders")

    query = executed_query(driver)
    assert "current_schemas(true)" in query


@pytest.mark.asyncio
async def test_column_stats_without_statistics():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=None)

    assert await StatisticsInspector(driver).column_stats("orders") == []


# -- Extended statistics ----------------------------------------------------


@pytest.mark.asyncio
async def test_extended_statistics_lists_objects():
    driver = make_driver([{"name": "orders_user_status", "columns": ["user_id", "status"], "kinds": ["d", "f", "m"]}])

    rows = await StatisticsInspector(driver).extended_statistics("public.orders")

    assert rows[0]["columns"] == ["user_id", "status"]
    assert "pg_statistic_ext" in executed_query(driver)


@pytest.mark.asyncio
async def test_extended_statistics_when_none_exist():
    driver = make_driver([])

    assert await StatisticsInspector(driver).extended_statistics("orders") == []


# -- Staleness --------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_tables_uses_the_configured_thresholds():
    driver = make_driver()

    await StatisticsInspector(driver).stale_tables(stale_fraction=0.5, min_changed_rows=100)

    query = executed_query(driver)
    assert "0.5" in query
    assert "100" in query


@pytest.mark.asyncio
async def test_stale_tables_also_reports_never_analyzed_tables():
    driver = make_driver()

    await StatisticsInspector(driver).stale_tables()

    query = executed_query(driver)
    # Whether a table has statistics is read from pg_stats, which a reset of
    # the counters leaves alone, and only of a table with data.
    assert "FROM pg_stats p" in query
    assert "pg_relation_size(s.relid) > 0" in query


# -- Health check -----------------------------------------------------------


@pytest.mark.asyncio
async def test_statistics_check_when_everything_is_current():
    with patch.object(StatisticsInspector, "stale_tables", AsyncMock(return_value=[])):
        result = await StatisticsHealthCalc(MagicMock(spec=SqlDriver)).stale_statistics_check()

    assert result.name == "Statistics health"
    assert result.healthy is True
    assert result.summary == "All tables have current statistics."
    assert result.metrics == {"stale_fraction": 0.2, "min_changed_rows": 1000}


@pytest.mark.asyncio
async def test_statistics_check_reports_drift():
    tables = [
        {
            "schema": "public",
            "table": "orders",
            "live_rows": 50000,
            "changed_rows": 25000,
            "changed_fraction": 0.5,
            "never_analyzed": False,
        }
    ]

    with patch.object(StatisticsInspector, "stale_tables", AsyncMock(return_value=tables)):
        result = await StatisticsHealthCalc(MagicMock(spec=SqlDriver)).stale_statistics_check()

    assert result.healthy is False
    assert "public.orders" in result.summary
    assert "25,000 rows changed" in result.summary
    assert "50.0% of the table" in result.summary
    assert result.findings == tables


@pytest.mark.asyncio
async def test_statistics_check_reports_never_analyzed_tables_differently():
    tables = [
        {
            "schema": "public",
            "table": "fresh",
            "live_rows": 5000,
            "changed_rows": 5000,
            "changed_fraction": 1.0,
            "never_analyzed": True,
            "size_bytes": 5 * 1024 * 1024,
        }
    ]

    with patch.object(StatisticsInspector, "stale_tables", AsyncMock(return_value=tables)):
        result = await StatisticsHealthCalc(MagicMock(spec=SqlDriver)).stale_statistics_check()

    assert "'public.fresh' has no planner statistics: it has never been analyzed with rows in it (5.0 MiB)" in result.summary


@pytest.mark.asyncio
async def test_statistics_check_passes_its_thresholds_through():
    calc = StatisticsHealthCalc(MagicMock(spec=SqlDriver), stale_fraction=0.5, min_changed_rows=10)

    with patch.object(StatisticsInspector, "stale_tables", AsyncMock(return_value=[])) as stale_tables:
        result = await calc.stale_statistics_check()

    stale_tables.assert_awaited_once_with(stale_fraction=0.5, min_changed_rows=10)
    assert result.metrics == {"stale_fraction": 0.5, "min_changed_rows": 10}


def test_statistics_is_part_of_all_checks():
    assert HealthType.STATISTICS in HealthType.parse("all")


@pytest.mark.asyncio
async def test_the_statistics_reads_run_in_restricted_mode():
    """Restricted mode validates a statement before it reaches the server."""
    from pg_debug.db import SafeSqlDriver
    from pg_debug.index.base import ANALYZED_SQL

    driver = make_driver()
    await StatisticsInspector(driver).stale_tables()

    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    safe._validate(executed_query(driver))
    safe._validate(ANALYZED_SQL)
