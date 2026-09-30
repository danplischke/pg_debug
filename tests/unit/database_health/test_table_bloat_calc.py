"""Table bloat is reported only when it is both large and a large share."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.database_health import DatabaseHealthTool
from pg_debug.database_health import HealthType
from pg_debug.database_health.table_bloat_calc import _RELATION_FILTER
from pg_debug.database_health.table_bloat_calc import DEFAULT_MIN_BLOAT_BYTES
from pg_debug.database_health.table_bloat_calc import DEFAULT_MIN_BLOAT_PERCENT
from pg_debug.database_health.table_bloat_calc import TABLE_BLOAT_SQL
from pg_debug.database_health.table_bloat_calc import TableBloatCalc
from pg_debug.db import SqlDriver
from pg_debug.tuple_layout import row_layout_ctes


def row(**cells):
    return SqlDriver.RowResult(cells=cells)


def driver_returning(rows):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=rows)
    return driver


@pytest.mark.asyncio
async def test_no_bloated_tables_is_healthy():
    check = await TableBloatCalc(driver_returning([])).table_bloat_check()

    assert check.healthy
    assert check.summary == "No bloated tables found."
    assert check.findings == []
    assert check.metrics == {
        "min_bloat_bytes": DEFAULT_MIN_BLOAT_BYTES,
        "min_bloat_percent": DEFAULT_MIN_BLOAT_PERCENT,
    }


@pytest.mark.asyncio
async def test_a_bloated_table_is_reported_with_its_numbers():
    rows = [
        row(
            schema="public",
            table="orders",
            bloat_bytes=419430400,  # 400 MB
            table_bytes=1073741824,  # 1 GB
            bloat_percent=39.1,
            live_rows=1000000.0,
            actual_pages=131072,
            expected_pages=79872,
        )
    ]

    check = await TableBloatCalc(driver_returning(rows)).table_bloat_check()

    assert not check.healthy
    assert "public.orders" in check.summary
    assert "400.0MB bloat out of 1024.0MB total size (39.1%)" in check.summary
    assert check.findings == [dict(rows[0].cells)]


@pytest.mark.asyncio
async def test_thresholds_are_passed_to_the_query_and_reported():
    driver = driver_returning([])

    check = await TableBloatCalc(driver).table_bloat_check(min_bloat_bytes=1024, min_bloat_percent=5.0)

    query = driver.execute_query.call_args[0][0]
    assert "1024" in query
    assert "5.0" in query
    assert check.metrics == {"min_bloat_bytes": 1024, "min_bloat_percent": 5.0}


@pytest.mark.asyncio
async def test_every_table_is_filtered_by_both_thresholds():
    """One threshold alone flags either every large table or every small one."""
    assert TABLE_BLOAT_SQL.count("{}") == 2
    where = TABLE_BLOAT_SQL.split("WHERE actual_pages > expected_pages")[1]
    assert ">= {}" in where
    assert " AND " in where


def test_the_check_reads_only_planner_statistics():
    """No extension required, so it can run anywhere."""
    assert "pgstattuple" not in TABLE_BLOAT_SQL
    assert "pg_stats" in TABLE_BLOAT_SQL
    assert "pg_class" in TABLE_BLOAT_SQL


def test_only_ordinary_and_materialized_relations_are_measured():
    """Indexes have their own check, and views have no heap."""
    assert "c.relkind IN ('r', 'm')" in TABLE_BLOAT_SQL


def test_the_row_width_comes_from_the_shared_layout():
    """Summing widths ignores the padding between columns and invents bloat.

    The layout itself is tested in tests/unit/test_tuple_layout.py; what
    matters here is that this check takes its row width from there rather than
    computing its own, so the two can never disagree.
    """
    assert row_layout_ctes(_RELATION_FILTER) in TABLE_BLOAT_SQL
    assert "s.row_bytes" in TABLE_BLOAT_SQL


def test_fillfactor_is_taken_from_the_table_options():
    """A table set to fill 70% of each page is not 30% bloated."""
    assert "fillfactor=%" in TABLE_BLOAT_SQL
    assert "e.fillfactor / 100" in TABLE_BLOAT_SQL


def test_a_page_holds_whole_rows_only():
    assert "floor((e.block_size" in TABLE_BLOAT_SQL


def test_tables_with_an_unanalyzed_column_are_skipped():
    """Their width would be a guess, and the guess would read as bloat."""
    assert "HAVING bool_and(analyzed)" in TABLE_BLOAT_SQL


def test_table_is_one_of_the_health_types():
    assert HealthType.TABLE in HealthType.parse("all")
    assert HealthType.parse("table") == {HealthType.TABLE}


@pytest.mark.asyncio
async def test_the_tool_dispatches_the_table_check():
    tool = DatabaseHealthTool(driver_returning([]))

    results = await tool.run_checks("table")

    assert list(results) == ["Table bloat"]
