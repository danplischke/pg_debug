"""Deciding how to run the statement, which is the part with consequences.

Everything else in a diagnosis is arithmetic over a tree. This is the part that
executes the user's query, so the decisions here — whether it needs undoing
afterwards, and whether it may run at all — are the ones worth pinning down.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import ExplainPlanError
from pg_debug.errors import InvalidArgumentError
from pg_debug.plans.diagnoser import PlanDiagnoser

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
