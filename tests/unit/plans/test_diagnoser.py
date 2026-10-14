"""Deciding how to run the statement, which is the part with consequences.

Everything else in a diagnosis is arithmetic over a tree. This is the part that
executes the user's query, so the decisions here — whether it needs undoing
afterwards, and whether it may run at all — are the ones worth pinning down.
"""

import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import ExplainPlanError
from pg_debug.errors import InvalidArgumentError
from pg_debug.plans.diagnoser import PlanDiagnoser
from pg_debug.plans.diagnoser import diagnose_plan
from pg_debug.plans.diagnoser import parse_plan
from pg_debug.plans.result import PlanIssue

PLAN = {
    "Plan": {
        "Node Type": "Seq Scan",
        "Total Cost": 10.0,
        "Startup Cost": 0.0,
        "Plan Rows": 100,
        "Plan Width": 8,
        "Actual Total Time": 1.0,
        "Actual Startup Time": 0.0,
        "Actual Rows": 100,
        "Actual Loops": 1,
    },
    "Execution Time": 1.2,
}


def driver_returning_a_plan():
    """A driver that answers an EXPLAIN with a plan and everything else with nothing."""
    driver = MagicMock(spec=SqlDriver)

    async def execute(query, *args, **kwargs):
        if "EXPLAIN" in query:
            return [SqlDriver.RowResult(cells={"QUERY PLAN": [PLAN]})]
        return []

    driver.execute_query = AsyncMock(side_effect=execute)
    driver.execute_query_and_rollback = AsyncMock(side_effect=execute)
    return driver


@pytest.mark.asyncio
async def test_restricted_mode_refuses_rather_than_failing_obscurely():
    """Restricted mode promises nothing executes, and EXPLAIN ANALYZE executes.
    Refusing up front with the reason beats letting the SQL validator reject
    the word ANALYZE."""
    with pytest.raises(InvalidArgumentError, match="restricted mode does not run queries"):
        await PlanDiagnoser(MagicMock(spec=SafeSqlDriver)).diagnose("SELECT 1")


@pytest.mark.asyncio
async def test_a_ratio_below_one_is_rejected():
    with pytest.raises(InvalidArgumentError, match="at least 1"):
        await PlanDiagnoser(driver_returning_a_plan()).diagnose("SELECT 1", min_ratio=0.5)


@pytest.mark.asyncio
async def test_a_script_is_rejected_rather_than_partly_run():
    with pytest.raises(InvalidArgumentError, match="one statement at a time"):
        await PlanDiagnoser(driver_returning_a_plan()).diagnose("SELECT 1; SELECT 2")


@pytest.mark.asyncio
async def test_a_select_is_simply_run():
    driver = driver_returning_a_plan()

    await PlanDiagnoser(driver).diagnose("SELECT * FROM visits")

    assert any("EXPLAIN" in call.args[0] for call in driver.execute_query.await_args_list)
    driver.execute_query_and_rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_update_is_run_inside_a_transaction_that_is_rolled_back():
    driver = driver_returning_a_plan()

    await PlanDiagnoser(driver).diagnose("UPDATE visits SET seen = true WHERE id = 1")

    driver.execute_query_and_rollback.assert_awaited_once()
    assert "EXPLAIN" in driver.execute_query_and_rollback.await_args.args[0]


@pytest.mark.asyncio
async def test_a_delete_is_rolled_back_too():
    driver = driver_returning_a_plan()

    await PlanDiagnoser(driver).diagnose("DELETE FROM visits WHERE id = 1")

    driver.execute_query_and_rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_statement_that_cannot_be_parsed_is_treated_as_a_write():
    """Guessing wrong this way costs a rolled-back transaction. Guessing wrong
    the other way costs the user their data.

    The statement is one the parser this library uses does not accept, which is
    the realistic case: a newer syntax, or an extension's own grammar.
    """
    driver = driver_returning_a_plan()

    await PlanDiagnoser(driver).diagnose("SELECT * FROM visits WHERE ((")

    driver.execute_query_and_rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_write_the_parser_does_understand_is_still_rolled_back():
    """Only SELECT is treated as read-only. A statement type this library has
    never heard of falls on the safe side by construction rather than by having
    been listed."""
    driver = driver_returning_a_plan()

    await PlanDiagnoser(driver).diagnose("MERGE INTO visits t USING (SELECT 1 AS id) s ON t.id = s.id WHEN NOT MATCHED THEN DO NOTHING")

    driver.execute_query_and_rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_plan_is_asked_for_verbose_so_a_relation_can_be_resolved():
    """Without VERBOSE the plan names a table and not its schema, and a name on
    its own cannot be looked up when two schemas hold one."""
    driver = driver_returning_a_plan()

    await PlanDiagnoser(driver).diagnose("SELECT 1")

    explain = next(call.args[0] for call in driver.execute_query.await_args_list if "EXPLAIN" in call.args[0])
    assert "ANALYZE" in explain and "VERBOSE" in explain and "FORMAT JSON" in explain


@pytest.mark.asyncio
async def test_a_failure_to_run_the_statement_is_reported_as_a_plan_error():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(side_effect=RuntimeError("relation does not exist"))

    with pytest.raises(ExplainPlanError, match="relation does not exist"):
        await PlanDiagnoser(driver).diagnose("SELECT * FROM nope")


@pytest.mark.asyncio
async def test_a_healthy_plan_diagnoses_to_nothing():
    assert await PlanDiagnoser(driver_returning_a_plan()).diagnose("SELECT * FROM visits") == []


# -- Plans that already exist ------------------------------------------------

MISESTIMATE = {
    "Plan": {
        "Node Type": "Seq Scan",
        "Relation Name": "visits",
        "Schema": "public",
        "Alias": "visits",
        "Filter": "((visits.country = 'c0'::text) AND (visits.city = 'city0'::text))",
        "Total Cost": 100.0,
        "Startup Cost": 0.0,
        "Plan Rows": 50,
        "Plan Width": 8,
        "Actual Total Time": 25.0,
        "Actual Startup Time": 0.0,
        "Actual Rows": 1000,
        "Actual Loops": 1,
    },
    "Execution Time": 26.0,
}


def test_a_plan_is_read_as_text_as_the_list_explain_prints_or_as_the_object_inside():
    for form in (json.dumps([PLAN]), json.dumps([PLAN]).encode(), [PLAN], PLAN):
        assert parse_plan(form).node_type == "Seq Scan"


def test_the_text_format_is_refused_with_the_options_that_would_work():
    with pytest.raises(InvalidArgumentError, match="FORMAT JSON"):
        parse_plan("Seq Scan on orders  (cost=0.00..1.00 rows=1 width=8) (actual time=0.01..0.02 rows=1 loops=1)")


def test_a_plan_that_was_never_run_is_refused():
    """Without ANALYZE there are estimates and nothing to compare them to."""
    with pytest.raises(InvalidArgumentError, match="never run"):
        parse_plan({"Plan": {"Node Type": "Seq Scan", "Total Cost": 1.0, "Startup Cost": 0.0, "Plan Rows": 1, "Plan Width": 8}})


def test_other_shapes_are_refused():
    with pytest.raises(InvalidArgumentError, match="list of 2"):
        parse_plan([PLAN, PLAN])
    with pytest.raises(InvalidArgumentError, match="'Plan' key"):
        parse_plan({"Query Text": "SELECT 1"})


def test_a_healthy_plan_diagnoses_to_nothing_with_no_database():
    assert diagnose_plan([PLAN]) == []


def test_a_misestimate_with_no_database_says_the_cause_was_not_checked():
    """Silence from the two catalog rules must not read as a clean bill."""
    (finding,) = diagnose_plan(MISESTIMATE)

    assert finding.issue is PlanIssue.ROW_ESTIMATE
    assert finding.relation == "public.visits"
    assert "needs this database's catalog" in finding.detail
    assert finding.evidence["catalog_checked"] is False


def test_the_top_level_function_is_the_same_thing():
    import pg_debug

    assert pg_debug.diagnose_plan is diagnose_plan


@pytest.mark.asyncio
async def test_a_driver_makes_the_catalog_questions_get_asked():
    driver = driver_returning_a_plan()

    (finding,) = await PlanDiagnoser(driver).diagnose_plan(MISESTIMATE)

    asked = [call.args[0] for call in driver.execute_query.await_args_list]
    assert any("pg_statistic_ext" in query for query in asked), "extended statistics were not looked up"
    assert any("pg_attribute" in query for query in asked), "the table's columns were not looked up"
    assert not any("EXPLAIN" in query for query in asked), "nothing should have been run"
    assert "catalog_checked" not in finding.evidence
    assert "needs this database's catalog" not in finding.detail


@pytest.mark.asyncio
async def test_a_plan_can_be_read_in_restricted_mode():
    """Reading runs nothing, so the mode that runs nothing allows it."""
    assert await PlanDiagnoser(SafeSqlDriver(driver_returning_a_plan())).diagnose_plan([PLAN]) == []


@pytest.mark.asyncio
async def test_running_a_query_needs_a_driver_and_says_what_does_not():
    with pytest.raises(InvalidArgumentError, match="diagnose_plan"):
        await PlanDiagnoser().diagnose("SELECT 1")


@pytest.mark.asyncio
async def test_restricted_mode_points_at_the_plan_route():
    with pytest.raises(InvalidArgumentError, match="--plan"):
        await PlanDiagnoser(SafeSqlDriver(driver_returning_a_plan())).diagnose("SELECT 1")


# -- The query context ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_statement_and_the_work_mem_it_ran_with_are_read_in_the_query_context():
    """The context is the application's role and settings: the statement runs
    under them, and a spill finding quotes the work_mem it ran with. The
    catalog is read as the connecting user, from whom row-level security hides
    no statistics."""
    driver = MagicMock(spec=SqlDriver)

    async def execute(query, *args, **kwargs):
        if "EXPLAIN" in query:
            return [SqlDriver.RowResult(cells={"QUERY PLAN": [MISESTIMATE]})]
        return []

    driver.execute_query = AsyncMock(side_effect=execute)

    await PlanDiagnoser(driver).diagnose("SELECT * FROM visits WHERE a = 1 AND b = 2")

    calls = driver.execute_query.await_args_list
    in_context = [call.args[0] for call in calls if call.kwargs.get("in_context")]
    outside = [call.args[0] for call in calls if not call.kwargs.get("in_context")]
    assert len(in_context) == 2
    assert in_context[0].startswith("EXPLAIN")
    assert "work_mem" in in_context[1]
    assert any("pg_statistic_ext" in query for query in outside)


@pytest.mark.asyncio
async def test_a_write_rolled_back_after_runs_in_the_query_context_too():
    driver = driver_returning_a_plan()

    await PlanDiagnoser(driver).diagnose("DELETE FROM visits WHERE id = 1")

    assert driver.execute_query_and_rollback.await_args.kwargs == {"in_context": True}
