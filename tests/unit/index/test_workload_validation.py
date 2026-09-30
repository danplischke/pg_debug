"""What the advisor does with workload entries it cannot use.

A workload read from pg_stat_statements is not all valid, plannable SQL, and
one entry that is not is no reason to abandon the rest — which is what an
uncaught error here used to do.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import QUERY_MARKER
from pg_debug.db import SqlDriver
from pg_debug.index.base import HIDDEN_QUERY_TEXT
from pg_debug.index.dta import DatabaseTuningAdvisor

USABLE = "select * from orders where user_id = 42"


@pytest.fixture
def advisor():
    """An advisor whose queries all parse and plan unless a test says otherwise."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])
    advisor = DatabaseTuningAdvisor(driver)
    advisor.get_explain_plan_with_indexes = AsyncMock(return_value={"Plan": {"Total Cost": 1.0}})
    advisor._sql_bind_params.replace_parameters = AsyncMock(side_effect=lambda text: text)
    return advisor


def entries(*queries):
    return [{"query": q, "queryid": i, "calls": 1, "avg_exec_time": 1.0} for i, q in enumerate(queries)]


@pytest.mark.asyncio
async def test_a_usable_query_is_kept(advisor):
    kept = await advisor._validate_and_parse_workload(entries(USABLE))

    assert [q["query"] for q in kept] == [USABLE]
    assert advisor._workload_warning is None


@pytest.mark.asyncio
async def test_hidden_query_text_is_skipped_with_the_grant_that_fixes_it(advisor):
    """pg_stat_statements shows this instead of another role's query text, and
    it is the reported cause of 'syntax error near <'."""
    kept = await advisor._validate_and_parse_workload(entries(HIDDEN_QUERY_TEXT, USABLE))

    assert [q["query"] for q in kept] == [USABLE]
    assert "pg_read_all_stats" in advisor._workload_warning


@pytest.mark.asyncio
async def test_an_unparseable_query_is_skipped_rather_than_raising(advisor):
    kept = await advisor._validate_and_parse_workload(entries("select * from orders where status = 'unterminat", USABLE))

    assert [q["query"] for q in kept] == [USABLE]
    assert "could not be parsed" in advisor._workload_warning


@pytest.mark.asyncio
async def test_a_query_the_server_will_not_plan_is_skipped(advisor):
    """Constants are stripped by pg_stat_statements and guessed back, and a
    guess of the wrong type plans as an error."""
    advisor.get_explain_plan_with_indexes = AsyncMock(
        side_effect=lambda query, indexes: (_ for _ in ()).throw(RuntimeError("operator does not exist")) if "enum_col" in query else {"Plan": {}}
    )

    kept = await advisor._validate_and_parse_workload(entries("select * from orders where enum_col = 44", USABLE))

    assert [q["query"] for q in kept] == [USABLE]
    assert "could not be planned" in advisor._workload_warning


@pytest.mark.asyncio
async def test_our_own_queries_are_not_part_of_the_workload(advisor):
    """pg_debug's catalog queries end up in pg_stat_statements like any other."""
    kept = await advisor._validate_and_parse_workload(entries(f"{QUERY_MARKER} select * from pg_class", USABLE))

    assert [q["query"] for q in kept] == [USABLE]
    # Our own noise is not something to report to the user.
    assert advisor._workload_warning is None


@pytest.mark.asyncio
async def test_the_counts_are_reported_together(advisor):
    kept = await advisor._validate_and_parse_workload(entries(HIDDEN_QUERY_TEXT, HIDDEN_QUERY_TEXT, "select from where", USABLE))

    assert len(kept) == 1
    warning = advisor._workload_warning
    assert "Analyzing 1 query" in warning
    assert "3 were skipped" in warning
    assert "2 came back" in warning


@pytest.mark.asyncio
async def test_an_entirely_unusable_workload_says_so(advisor):
    """'No index recommendations' would read as 'your indexes are fine'."""
    kept = await advisor._validate_and_parse_workload(entries(HIDDEN_QUERY_TEXT, HIDDEN_QUERY_TEXT))

    assert kept == []
    assert advisor._workload_warning.startswith("None of the 2 queries in the workload could be analyzed")


def test_one_skipped_query_is_not_described_in_the_plural():
    assert "1 was skipped" in (DatabaseTuningAdvisor._describe_dropped_queries({"hidden": 1}, kept=2) or "")
    assert "2 were skipped" in (DatabaseTuningAdvisor._describe_dropped_queries({"hidden": 2}, kept=2) or "")


def test_nothing_is_said_when_nothing_was_dropped():
    assert DatabaseTuningAdvisor._describe_dropped_queries({}, kept=5) is None


# -- Relations that cannot carry an index -----------------------------------


@pytest.mark.asyncio
async def test_only_indexable_relations_are_kept(advisor):
    """HypoPG raises on a view rather than declining it, and that used to abort
    the whole analysis over one perfectly ordinary query."""
    advisor.sql_driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"name": "orders"})])

    assert await advisor.indexable_tables(["orders", "paid_orders"]) == {"orders"}


@pytest.mark.asyncio
async def test_nothing_is_asked_of_the_server_for_an_empty_workload(advisor):
    advisor.sql_driver.execute_query = AsyncMock(return_value=[])

    assert await advisor.indexable_tables([]) == set()
    advisor.sql_driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_query_asks_for_the_kinds_an_index_can_go_on(advisor):
    """Ordinary tables, materialized views and partitioned tables — not views."""
    advisor.sql_driver.execute_query = AsyncMock(return_value=[])

    await advisor.indexable_tables(["orders"])

    query = advisor.sql_driver.execute_query.call_args[0][0]
    assert "to_regclass" in query, "names must resolve through the search_path, as the query itself would"
    assert "relkind = ANY" in query
    assert "'{r,m,p}'" in query
    assert "information_schema" in query, "an index on a catalog table is not a recommendation"
