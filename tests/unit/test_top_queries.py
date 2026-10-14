from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

import pg_debug.top_queries as top_queries_module
from pg_debug.db import SqlDriver
from pg_debug.db.extensions import ExtensionStatus
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.db.privileges import StatisticsAccess
from pg_debug.errors import ExtensionRequiredError
from pg_debug.top_queries import PG_STAT_STATEMENTS
from pg_debug.top_queries import TopQueriesCalc


class MockSqlRowResult:
    def __init__(self, cells):
        self.cells = cells


# Fixtures for different PostgreSQL versions
@pytest.fixture
def mock_pg12_driver():
    """Create a mock for SqlDriver that simulates PostgreSQL 12."""
    driver = MagicMock(spec=SqlDriver)

    # Set up the version mock directly on the mock driver
    with patch.object(top_queries_module, "get_postgres_version", autospec=True) as mock_version:
        mock_version.return_value = 12

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
    """Create a mock for SqlDriver that simulates PostgreSQL 13."""
    driver = MagicMock(spec=SqlDriver)

    # Set up the version mock directly on the mock driver
    with patch.object(top_queries_module, "get_postgres_version", autospec=True) as mock_version:
        mock_version.return_value = 13

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


# Patch check_extension to return different extension statuses
@pytest.fixture
def mock_extension_installed():
    """Mock check_extension to report extension is installed."""
    with patch.object(top_queries_module, "check_extension", autospec=True) as mock_check:
        mock_check.return_value = ExtensionStatus(
            is_installed=True,
            is_available=True,
            name="pg_stat_statements",
            message="Extension is installed",
            default_version="1.0",
        )
        yield mock_check


@pytest.fixture
def mock_extension_not_installed():
    """Mock check_extension to report extension is not installed."""
    with patch.object(top_queries_module, "check_extension", autospec=True) as mock_check:
        mock_check.return_value = ExtensionStatus(
            is_installed=False,
            is_available=True,
            name="pg_stat_statements",
            message="Extension not installed",
            default_version=None,
        )
        yield mock_check


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


# -- Text pg_stat_statements withholds from the role -------------------------


def recorded(*texts):
    """A PostgreSQL 16 driver whose pg_stat_statements holds these query texts."""
    driver = MagicMock(spec=SqlDriver)

    async def execute(query, *args, **kwargs):
        if "pg_stat_statements" in query:
            return [
                MockSqlRowResult(cells={"query": text, "calls": 10, "total_exec_time": 100.0, "mean_exec_time": 10.0, "rows": 1}) for text in texts
            ]
        return None

    driver.execute_query = AsyncMock(side_effect=execute)
    return driver


@pytest.fixture
def pg16():
    with patch.object(top_queries_module, "get_postgres_version", AsyncMock(return_value=16)):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("texts", "said"),
    [
        ([HIDDEN_QUERY_TEXT, HIDDEN_QUERY_TEXT, "SELECT 1"], "2 of these queries are other roles': pg_stat_statements shows app_user their text"),
        ([HIDDEN_QUERY_TEXT, "SELECT 1"], "1 of these queries is another role's: pg_stat_statements shows app_user its text"),
    ],
)
async def test_queries_shown_without_their_text_are_counted_and_explained(texts, said, pg16, mock_extension_installed):
    """Only the index advisor used to say so; top-queries listed the
    '<insufficient privilege>' rows and left the reader to guess why."""
    access = StatisticsAccess(role="app_user", reads_all_stats=False)
    with patch.object(top_queries_module, "statistics_access", AsyncMock(return_value=access)):
        result = await TopQueriesCalc(recorded(*texts)).get_top_queries_by_time(limit=10, sort_by="total")

    assert [row["query"] for row in result] == texts
    assert result.hidden is not None
    assert result.hidden.startswith(said)
    assert "pg_read_all_stats, which pg_monitor includes" in result.hidden
    assert result.hidden_count == texts.count(HIDDEN_QUERY_TEXT)


@pytest.mark.asyncio
async def test_the_resource_ranking_says_so_too(pg16, mock_extension_installed):
    access = StatisticsAccess(role="app_user", reads_all_stats=False)
    with patch.object(top_queries_module, "statistics_access", AsyncMock(return_value=access)):
        result = await TopQueriesCalc(recorded(HIDDEN_QUERY_TEXT)).get_top_resource_queries()

    assert result.hidden is not None and result.hidden_count == 1


@pytest.mark.asyncio
async def test_a_sample_says_so_too(mock_extension_installed):
    access = StatisticsAccess(role="app_user", reads_all_stats=False)
    snapshots = [
        {(1, 10, 1): {"queryid": 1, "query": HIDDEN_QUERY_TEXT, "calls": 1, "total_exec_time": 1.0}},
        {(1, 10, 1): {"queryid": 1, "query": HIDDEN_QUERY_TEXT, "calls": 3, "total_exec_time": 5.0}},
    ]
    with (
        patch.object(top_queries_module, "statistics_access", AsyncMock(return_value=access)),
        patch.object(TopQueriesCalc, "_counter_snapshot", AsyncMock(side_effect=snapshots)),
        patch("pg_debug.top_queries.asyncio.sleep", AsyncMock()),
    ):
        result = await TopQueriesCalc(MagicMock(spec=SqlDriver)).sample(1)

    assert result.hidden is not None and result.hidden.startswith("1 of these queries is another role's")


@pytest.mark.asyncio
async def test_the_server_is_asked_only_when_some_text_was_withheld(pg16, mock_extension_installed):
    with patch.object(top_queries_module, "statistics_access", AsyncMock()) as asked:
        result = await TopQueriesCalc(recorded("SELECT 1")).get_top_queries_by_time(limit=10)

    assert result.hidden is None
    asked.assert_not_awaited()
