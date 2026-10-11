"""Real plans, diagnosed with no database behind them.

The JSON under ``plans/`` is ``EXPLAIN (ANALYZE, VERBOSE, BUFFERS, FORMAT JSON)``
output taken from PostgreSQL 16 against a 200,000-row ``visits`` table whose
``city`` column determines its ``country``. It is what a user hands over when
they have a plan and not a database, and these pin what the rules say about it
without one — and what they say once the catalog can be asked.
"""

from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SqlDriver
from pg_debug.plans.diagnoser import PlanDiagnoser
from pg_debug.plans.diagnoser import diagnose_plan
from pg_debug.plans.result import PlanIssue

CORPUS = Path(__file__).parent / "plans"


def plan(name: str) -> str:
    return (CORPUS / f"{name}.json").read_text()


def test_the_correlated_columns_plan_reports_the_misestimate_and_what_was_not_checked():
    """Two columns, each restricting the scan; the planner multiplied their
    selectivities and expected 50 rows where 1,000 came back."""
    findings = diagnose_plan(plan("correlated_columns"))

    issues = [finding.issue for finding in findings]
    assert PlanIssue.ROW_ESTIMATE in issues
    assert PlanIssue.CORRELATED_COLUMNS not in issues, "that needs the catalog, and there is none"

    (estimate,) = [finding for finding in findings if finding.issue is PlanIssue.ROW_ESTIMATE]
    assert estimate.relation == "public.visits"
    assert estimate.evidence["estimated_rows"] == 50
    assert estimate.evidence["actual_rows"] == 1000
    assert estimate.evidence["catalog_checked"] is False
    assert "needs this database's catalog" in estimate.detail


@pytest.mark.asyncio
async def test_with_the_catalog_the_correlation_is_named_and_the_fix_is_a_statement():
    """The same plan, with a driver that knows the table's columns and that
    nothing in pg_statistic_ext covers them."""
    driver = MagicMock(spec=SqlDriver)

    async def execute(query, *args, **kwargs):
        if "pg_attribute" in query and "attisdropped" in query:
            return [SqlDriver.RowResult(cells={"schema": "public", "table": "visits", "column": c}) for c in ("id", "country", "city", "hits")]
        return []

    driver.execute_query = AsyncMock(side_effect=execute)

    findings = await PlanDiagnoser(driver).diagnose_plan(plan("correlated_columns"))

    (correlated,) = [finding for finding in findings if finding.issue is PlanIssue.CORRELATED_COLUMNS]
    assert correlated.evidence["columns"] == ["country", "city"]
    assert correlated.remediation[0] == "CREATE STATISTICS public.visits_country_city_stx (dependencies, mcv) ON country, city FROM public.visits;"


def test_the_spilled_sort_is_reported_from_the_plan_alone():
    findings = diagnose_plan(plan("sort_spilled"))

    (spill,) = [finding for finding in findings if finding.issue is PlanIssue.SORT_SPILLED]
    assert spill.node_type == "Sort"
    assert any("work_mem" in step for step in spill.remediation)


def test_the_healthy_plan_diagnoses_to_nothing():
    assert diagnose_plan(plan("healthy")) == []
