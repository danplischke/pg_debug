from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

import pg_debug.top_queries as top_queries_module
from pg_debug.db import SqlDriver
from pg_debug.db.statements import StatementStatistics
from pg_debug.errors import ExtensionRequiredError
from pg_debug.top_queries import PG_STAT_STATEMENTS
from pg_debug.top_queries import TopQueriesCalc


class MockSqlRowResult:
    def __init__(self, cells):
        self.cells = cells


# Fixtures for different PostgreSQL versions
@pytest.fixture
def mock_pg12_driver():
    """Create a mock for SqlDriver that simulates pg_stat_statements 1.7, as PostgreSQL 12 has it."""
    driver = MagicMock(spec=SqlDriver)

    # Its columns follow the extension's version
    with patch.object(top_queries_module, "locate_pg_stat_statements", autospec=True) as locate:
        locate.return_value = StatementStatistics(relation="public.pg_stat_statements", version=(1, 7))

        # Create async mock for execute_query
        mock_execute = AsyncMock()

        # Configure the mock to return different results based on the query
        async def side_effect(query, *args, **kwargs):
            if "pg_stat_statements" in query:
                # Return data in PG 12 format with total_time and mean_time columns
                return [
                    MockSqlRowResult(cells={"query": "SELECT * FROM users", "calls": 100, "total_time": 1000.0, "mean_time": 10.0, "rows": 1000}),
                    MockSqlRowResult(cells={"query": "SELECT * FROM orders", "calls": 50, "total_time": 750.0, "mean_time": 15.0, "rows": 500}),
                    MockSqlRowResult(cells={"query": "SELECT * FROM products", "calls": 200, "total_time": 500.0, "mean_time": 2.5, "rows": 2000}),
                ]
            return None

        mock_execute.side_effect = side_effect
        driver.execute_query = mock_execute

        yield driver


@pytest.fixture
def mock_pg13_driver():
    """Create a mock for SqlDriver that simulates pg_stat_statements 1.8, as PostgreSQL 13 has it."""
    driver = MagicMock(spec=SqlDriver)

    # Its columns follow the extension's version
    with patch.object(top_queries_module, "locate_pg_stat_statements", autospec=True) as locate:
        locate.return_value = StatementStatistics(relation="public.pg_stat_statements", version=(1, 8))

        # Create async mock for execute_query
        mock_execute = AsyncMock()

        # Configure the mock to return different results based on the query
        async def side_effect(query, *args, **kwargs):
            if "pg_stat_statements" in query:
                # Return data in PG 13+ format with total_exec_time and mean_exec_time columns
                return [
                    MockSqlRowResult(
                        cells={"query": "SELECT * FROM users", "calls": 100, "total_exec_time": 1000.0, "mean_exec_time": 10.0, "rows": 1000}
                    ),
                    MockSqlRowResult(
                        cells={"query": "SELECT * FROM orders", "calls": 50, "total_exec_time": 750.0, "mean_exec_time": 15.0, "rows": 500}
                    ),
                    MockSqlRowResult(
                        cells={"query": "SELECT * FROM products", "calls": 200, "total_exec_time": 500.0, "mean_exec_time": 2.5, "rows": 2000}
                    ),
                ]
            return None

        mock_execute.side_effect = side_effect
        driver.execute_query = mock_execute

        yield driver


@pytest.fixture
def mock_extension_installed():
    """pg_stat_statements is installed: the driver fixture has found it."""
    yield


@pytest.fixture
def mock_extension_not_installed():
    """pg_stat_statements is not installed in this database."""
    with patch.object(top_queries_module, "locate_pg_stat_statements", AsyncMock(return_value=None)) as locate:
        yield locate


@pytest.mark.asyncio
async def test_top_queries_pg12_total_sort(mock_pg12_driver, mock_extension_installed):
    """Test top queries calculation on PostgreSQL 12 sorted by total execution time."""
    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg12_driver)

    # Get top queries sorted by total time
    result = await calc.get_top_queries_by_time(limit=3, sort_by="total")

    # Check that the rows are returned as structured data
    assert [row["query"] for row in result] == ["SELECT * FROM users", "SELECT * FROM orders", "SELECT * FROM products"]
    # Verify the query used the correct column name for PG 12
    assert "total_time" in str(mock_pg12_driver.execute_query.call_args)
    assert "ORDER BY total_time DESC" in str(mock_pg12_driver.execute_query.call_args)


@pytest.mark.asyncio
async def test_top_queries_pg12_mean_sort(mock_pg12_driver, mock_extension_installed):
    """Test top queries calculation on PostgreSQL 12 sorted by mean execution time."""
    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg12_driver)

    # Get top queries sorted by mean time
    result = await calc.get_top_queries_by_time(limit=3, sort_by="mean")

    # Check that the rows are returned as structured data
    assert [row["query"] for row in result] == ["SELECT * FROM users", "SELECT * FROM orders", "SELECT * FROM products"]
    # Verify the query used the correct column name for PG 12
    assert "mean_time" in str(mock_pg12_driver.execute_query.call_args)
    assert "ORDER BY mean_time DESC" in str(mock_pg12_driver.execute_query.call_args)


@pytest.mark.asyncio
async def test_top_queries_pg13_total_sort(mock_pg13_driver, mock_extension_installed):
    """Test top queries calculation on PostgreSQL 13 sorted by total execution time."""
    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg13_driver)

    # Get top queries sorted by total time
    result = await calc.get_top_queries_by_time(limit=3, sort_by="total")

    # Check that the rows are returned as structured data
    assert [row["query"] for row in result] == ["SELECT * FROM users", "SELECT * FROM orders", "SELECT * FROM products"]
    # Verify the query used the correct column name for PG 13+
    assert "total_exec_time" in str(mock_pg13_driver.execute_query.call_args)
    assert "ORDER BY total_exec_time DESC" in str(mock_pg13_driver.execute_query.call_args)


@pytest.mark.asyncio
async def test_top_queries_pg13_mean_sort(mock_pg13_driver, mock_extension_installed):
    """Test top queries calculation on PostgreSQL 13 sorted by mean execution time."""
    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg13_driver)

    # Get top queries sorted by mean time
    result = await calc.get_top_queries_by_time(limit=3, sort_by="mean")

    # Check that the rows are returned as structured data
    assert [row["query"] for row in result] == ["SELECT * FROM users", "SELECT * FROM orders", "SELECT * FROM products"]
    # Verify the query used the correct column name for PG 13+
    assert "mean_exec_time" in str(mock_pg13_driver.execute_query.call_args)
    assert "ORDER BY mean_exec_time DESC" in str(mock_pg13_driver.execute_query.call_args)


@pytest.mark.asyncio
async def test_extension_not_installed(mock_pg13_driver, mock_extension_not_installed):
    """Test behavior when pg_stat_statements extension is not installed."""
    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg13_driver)

    # The missing extension is reported as an error, with installation instructions
    with pytest.raises(ExtensionRequiredError) as excinfo:
        await calc.get_top_queries_by_time(limit=3)

    assert excinfo.value.extension == PG_STAT_STATEMENTS
    assert "extension is required to report" in str(excinfo.value)
    assert "CREATE EXTENSION" in str(excinfo.value)

    # Verify that execute_query was not called (since extension is not installed)
    mock_pg13_driver.execute_query.assert_not_called()


@pytest.mark.asyncio
async def test_error_handling(mock_pg13_driver, mock_extension_installed):
    """Test error handling in the TopQueriesCalc class."""
    # Configure execute_query to raise an exception
    mock_pg13_driver.execute_query.side_effect = Exception("Database error")

    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg13_driver)

    # The driver error propagates to the caller
    with pytest.raises(Exception, match="Database error"):
        await calc.get_top_queries_by_time(limit=3)


@pytest.mark.asyncio
async def test_resource_queries_pg12(mock_pg12_driver, mock_extension_installed):
    """Test resource queries on PostgreSQL 12 uses correct column names (stddev_time, no wal_bytes)."""
    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg12_driver)

    # Get resource queries
    _result = await calc.get_top_resource_queries(frac_threshold=0.05)

    # Get the executed query from the mock
    call_args = str(mock_pg12_driver.execute_query.call_args)

    # Verify PG12 column names are used (aliased to consistent output names)
    assert "stddev_time AS stddev_exec_time" in call_args, "Should use stddev_time aliased for PG12"
    assert "total_time AS total_exec_time" in call_args, "Should use total_time aliased for PG12"
    assert "mean_time AS mean_exec_time" in call_args, "Should use mean_time aliased for PG12"
    # wal_bytes should be replaced with 0 for PG12
    assert "0 AS wal_bytes" in call_args, "Should use 0 AS wal_bytes for PG12"


@pytest.mark.asyncio
async def test_resource_queries_pg13(mock_pg13_driver, mock_extension_installed):
    """Test resource queries on PostgreSQL 13 uses correct column names (stddev_exec_time, wal_bytes)."""
    # Create the TopQueriesCalc instance with the mock driver
    calc = TopQueriesCalc(sql_driver=mock_pg13_driver)

    # Get resource queries
    _result = await calc.get_top_resource_queries(frac_threshold=0.05)

    # Get the executed query from the mock
    call_args = str(mock_pg13_driver.execute_query.call_args)

    # Verify PG13 column names are used
    assert "stddev_exec_time" in call_args, "Should use stddev_exec_time for PG13"
    assert "total_exec_time" in call_args, "Should use total_exec_time for PG13"
    assert "mean_exec_time" in call_args, "Should use mean_exec_time for PG13"
    # wal_bytes should be the actual column for PG13
    assert "wal_bytes" in call_args, "Should use wal_bytes column for PG13"


@pytest.mark.asyncio
async def test_the_resource_ranking_takes_a_limit_and_ranks_by_the_largest_share(mock_pg13_driver, mock_extension_installed):
    """--limit did nothing here, and the rows came in order of execution time."""
    calc = TopQueriesCalc(sql_driver=mock_pg13_driver)

    await calc.get_top_resource_queries(limit=3)
    limited = str(mock_pg13_driver.execute_query.call_args)
    await calc.get_top_resource_queries()
    unlimited = str(mock_pg13_driver.execute_query.call_args)

    assert "LIMIT 3" in limited
    assert "LIMIT" not in unlimited
    assert "ORDER BY GREATEST(" in limited
