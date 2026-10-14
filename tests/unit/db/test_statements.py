"""pg_stat_statements as the server has it: where, which columns, and which rows are this database's."""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.statements import LOCATE_SQL
from pg_debug.db.statements import THIS_DATABASE
from pg_debug.db.statements import StatementStatistics
from pg_debug.db.statements import _version
from pg_debug.db.statements import locate_pg_stat_statements
from pg_debug.errors import ExtensionRequiredError
from pg_debug.index.dta import DatabaseTuningAdvisor
from pg_debug.partition.advisor import PartitionAdvisor
from pg_debug.top_queries import TopQueriesCalc


def answering(schema: str | None, version: str = "1.10", rows: list[dict[str, Any]] | None = None) -> MagicMock:
    """A driver that has pg_stat_statements in ``schema``, or not at all, and records what it was asked."""
    driver = MagicMock(spec=SqlDriver)

    async def execute_query(query, *args, **kwargs):
        if "FROM pg_extension e" in query:
            return [SqlDriver.RowResult(cells={"schema": schema, "version": version})] if schema else []
        return [SqlDriver.RowResult(cells=row) for row in rows or []]

    driver.execute_query = AsyncMock(side_effect=execute_query)
    return driver


def asked(driver: MagicMock) -> list[str]:
    return [call.args[0] for call in driver.execute_query.call_args_list]


@pytest.mark.parametrize(("text", "version"), [("1.10", (1, 10)), ("1.9", (1, 9)), ("1.7", (1, 7)), ("1.11devel", (1, 11))])
def test_a_version_compares_as_a_version_not_as_text(text, version):
    """As text, '1.10' sorts before '1.9'."""
    assert _version(text) == version


@pytest.mark.parametrize(
    ("version", "total", "wal", "toplevel"),
    [((1, 7), "total_time", False, False), ((1, 8), "total_exec_time", True, False), ((1, 10), "total_exec_time", True, True)],
)
def test_the_columns_follow_the_extension_s_version(version, total, wal, toplevel):
    """Not the server's: a cluster upgraded without ALTER EXTENSION ... UPDATE keeps the old view."""
    stats = StatementStatistics(relation="public.pg_stat_statements", version=version)

    assert (stats.total_time, stats.has_wal, stats.has_toplevel) == (total, wal, toplevel)


@pytest.mark.asyncio
async def test_the_view_is_named_with_the_schema_it_was_installed_in():
    stats = await locate_pg_stat_statements(answering('"Monitoring"', "1.9"))

    assert stats == StatementStatistics(relation='"Monitoring".pg_stat_statements', version=(1, 9))


@pytest.mark.asyncio
async def test_an_extension_not_installed_here_is_none():
    assert await locate_pg_stat_statements(answering(None)) is None


def test_the_lookup_and_the_database_filter_run_in_restricted_mode():
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    safe._validate(SafeSqlDriver.param_sql_to_query(LOCATE_SQL, ["pg_stat_statements"]))
    safe._validate(f"SELECT 1 FROM extensions.pg_stat_statements WHERE {THIS_DATABASE}")


# -- Every reader ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        lambda driver: TopQueriesCalc(driver).get_top_queries_by_time(),
        lambda driver: TopQueriesCalc(driver).get_top_resource_queries(),
        lambda driver: TopQueriesCalc(driver)._counter_snapshot(StatementStatistics("extensions.pg_stat_statements", (1, 10))),
        lambda driver: DatabaseTuningAdvisor(driver)._get_query_stats(1, 0, 10),
        lambda driver: PartitionAdvisor(driver)._workload(None, 10),
    ],
    ids=["by-time", "resources", "sample", "tune-workload", "partitions"],
)
async def test_every_reader_reads_this_database_s_rows_from_the_view_where_it_is(read):
    """Supabase installs it in the schema extensions, which a --search-path
    need not include; and its rows of another database name tables this one
    does not have."""
    driver = answering("extensions")

    await read(driver)

    (statement,) = [query for query in asked(driver) if "pg_stat_statements" in query and "pg_extension" not in query]
    assert "FROM extensions.pg_stat_statements" in statement
    assert THIS_DATABASE in statement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        lambda driver: DatabaseTuningAdvisor(driver)._get_query_stats(1, 0, 10),
        lambda driver: PartitionAdvisor(driver)._workload(None, 10),
    ],
    ids=["tune-workload", "partitions"],
)
async def test_the_advisors_read_the_workload_with_the_columns_postgresql_12_has(read):
    """They asked for total_exec_time on every version, which 1.7 does not have."""
    driver = answering("public", "1.7")

    await read(driver)

    (statement,) = [query for query in asked(driver) if "pg_stat_statements" in query and "pg_extension" not in query]
    assert "total_exec_time" not in statement.replace("AS total_exec_time", "")
    assert "total_time" in statement


@pytest.mark.asyncio
async def test_tune_workload_without_pg_stat_statements_says_so():
    """It returned no workload, which reads as nothing to recommend."""
    with pytest.raises(ExtensionRequiredError, match="CREATE EXTENSION pg_stat_statements"):
        await DatabaseTuningAdvisor(answering(None))._get_query_stats(1, 0, 10)


# -- Sampling ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "selected"), [((1, 10), "toplevel,"), ((1, 8), "NULL AS toplevel,")])
async def test_a_sample_reads_toplevel_where_the_view_has_it(version, selected):
    driver = answering("public")

    await TopQueriesCalc(driver)._counter_snapshot(StatementStatistics("public.pg_stat_statements", version))

    assert selected in " ".join(asked(driver)[-1].split())


def test_a_statement_run_at_top_level_and_inside_a_function_are_sampled_apart():
    """They are two rows with one queryid, user and database. Keyed without
    toplevel, the second overwrote the first, and its counters were
    subtracted from the other's."""
    row = {"queryid": 7, "userid": 10, "dbid": 5, "query": "SELECT 1", "calls": 1, "total_exec_time": 1.0}
    before = [{**row, "toplevel": True, "calls": 100}, {**row, "toplevel": False, "calls": 3}]
    after = [{**row, "toplevel": True, "calls": 110}, {**row, "toplevel": False, "calls": 4}]

    deltas = TopQueriesCalc._diff_snapshots(TopQueriesCalc._by_statement(before), TopQueriesCalc._by_statement(after))

    assert sorted(delta["calls"] for delta in deltas) == [1, 10]


@pytest.mark.asyncio
async def test_a_sample_finds_the_view_once():
    calc = TopQueriesCalc(answering("public"))

    with patch("pg_debug.top_queries.asyncio.sleep", AsyncMock()):
        await calc.measure(1)

    assert sum("FROM pg_extension e" in query for query in asked(calc.sql_driver)) == 1  # type: ignore[arg-type]
