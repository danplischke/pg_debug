"""The newer plan rules, each fired at a real plan and kept quiet on the others.

Every plan under ``plans/`` was taken from PostgreSQL 16 with ``EXPLAIN
(ANALYZE, VERBOSE, BUFFERS, FORMAT JSON)``, except ``generic_plan``, which is
``auto_explain`` output of a prepared statement under ``plan_cache_mode =
force_generic_plan``. The RLS plans were taken as a role the policy applies
to; with no catalog behind them their rules stay silent, and a driver that
answers the catalog's questions makes them speak.
"""

from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SqlDriver
from pg_debug.plans.control import OFFLINE_ADVICE
from pg_debug.plans.diagnoser import PlanDiagnoser
from pg_debug.plans.diagnoser import diagnose_plan
from pg_debug.plans.result import PlanIssue
from pg_debug.platform import Basis
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection
from pg_debug.severity import Confidence

CORPUS = Path(__file__).parent / "plans"


def plan(name: str) -> str:
    return (CORPUS / f"{name}.json").read_text()


def issues(findings) -> set[PlanIssue]:
    return {finding.issue for finding in findings}


def only(findings, issue: PlanIssue):
    (finding,) = [finding for finding in findings if finding.issue is issue]
    return finding


#: Each plan, and the rule written for it. Every other new rule must stay
#: silent on it: a rule that fires on its neighbours' plans is noise.
TARGETS = {
    "jit": PlanIssue.JIT_TIME,
    "triggers": PlanIssue.TRIGGER_TIME,
    "heap_fetches": PlanIssue.HEAP_FETCHES,
    "sparse_heap": PlanIssue.SPARSE_HEAP,
    "planning_time": PlanIssue.PLANNING_TIME,
    "cte_materialized": PlanIssue.CTE_MATERIALIZED,
    "large_offset": PlanIssue.LARGE_OFFSET,
    "or_join": PlanIssue.OR_ACROSS_JOIN,
    "generic_plan": PlanIssue.GENERIC_PLAN,
}
NEW_RULES = set(TARGETS.values()) | {
    PlanIssue.LIMIT_SCAN_FILTER,
    PlanIssue.FUNCTION_ON_INDEXED_COLUMN,
    PlanIssue.RLS_PER_ROW_CALL,
    PlanIssue.RLS_LEAKY_PREDICATE,
}


@pytest.mark.parametrize("name", [*TARGETS, "order_by_limit", "expression_filter", "rls_per_row", "rls_leaky", "healthy", "sort_spilled"])
def test_each_new_rule_fires_on_its_plan_and_on_no_other(name):
    found = issues(diagnose_plan(plan(name))) & NEW_RULES
    expected = {TARGETS[name]} if name in TARGETS else set()
    if name == "order_by_limit":
        expected = {PlanIssue.LIMIT_SCAN_FILTER}
    assert found == expected


def test_jit_is_a_share_of_the_execution_and_says_what_to_raise():
    finding = only(diagnose_plan(plan("jit")), PlanIssue.JIT_TIME)
    assert finding.cost_ms == pytest.approx(192.146)
    assert finding.share_of_time == pytest.approx(192.146 / 228.222)
    assert finding.evidence["share_of"] == "execution_time"
    assert finding.remediation[0].startswith("SET jit_above_cost = 100000;")
    assert finding.confidence is Confidence.PROVEN


def test_a_trigger_is_charged_against_the_execution_its_nodes_do_not_include():
    finding = only(diagnose_plan(plan("triggers")), PlanIssue.TRIGGER_TIME)
    assert finding.share_of_time > 0.99
    assert finding.evidence["constraint"] == "children_parent_id_fkey"
    assert "needs the catalog" in finding.detail
    assert finding.remediation == []


def test_heap_fetches_recommend_a_vacuum_of_the_table():
    finding = only(diagnose_plan(plan("heap_fetches")), PlanIssue.HEAP_FETCHES)
    assert finding.evidence["heap_fetches"] == 149998
    assert finding.remediation == ['VACUUM (ANALYZE) "public"."counters";']


def test_a_sparse_table_is_a_candidate_with_the_statement_that_confirms_it():
    finding = only(diagnose_plan(plan("sparse_heap")), PlanIssue.SPARSE_HEAP)
    assert finding.confidence is Confidence.CANDIDATE
    assert finding.evidence["rows_per_page"] < 4
    assert "pgstattuple" in finding.remediation[0]


def test_a_cte_scan_gets_the_cte_finding_and_no_index_advice():
    """No index can serve a CTE Scan; the CTE's own query is where one can."""
    findings = diagnose_plan(plan("cte_materialized"))
    finding = only(findings, PlanIssue.CTE_MATERIALIZED)
    assert finding.relation == "u"
    assert finding.remediation == ["-- WITH u AS NOT MATERIALIZED (...)"]
    assert PlanIssue.DISCARDED_ROWS not in issues(findings)


def test_an_offset_is_charged_only_for_the_rows_it_skipped():
    finding = only(diagnose_plan(plan("large_offset")), PlanIssue.LARGE_OFFSET)
    assert finding.evidence["rows_skipped"] == 200000
    assert finding.evidence["rows_returned"] == 20
    assert finding.cost_ms < 24.305


def test_the_order_by_limit_scan_replaces_the_generic_index_advice_and_carries_plan_control():
    findings = diagnose_plan(plan("order_by_limit"))
    finding = only(findings, PlanIssue.LIMIT_SCAN_FILTER)
    assert finding.evidence["index"] == "users_created_at_idx"
    assert finding.evidence["rows_removed"] == 299700
    assert PlanIssue.DISCARDED_ROWS not in issues(findings)
    assert finding.plan_control == [OFFLINE_ADVICE]


def test_the_generic_plan_quotes_the_parameters_auto_explain_logged():
    finding = only(diagnose_plan(plan("generic_plan")), PlanIssue.GENERIC_PLAN)
    assert finding.evidence["parameters"] == ["$1"]
    assert finding.evidence["query_parameters"] == "$1 = 'queued'"
    assert finding.remediation == ["SET plan_cache_mode = force_custom_plan;"]


def test_an_initplan_parameter_is_not_taken_for_a_generic_plan():
    """The expression_filter and cte plans have no $n; this one has the
    InitPlan's $0 in an Append's children, which is the plan's own."""
    text = plan("planning_time")
    assert PlanIssue.GENERIC_PLAN not in issues(diagnose_plan(text))


def test_the_or_join_is_a_candidate_naming_both_sides():
    finding = only(diagnose_plan(plan("or_join")), PlanIssue.OR_ACROSS_JOIN)
    assert finding.evidence["outer"] == ["e1"] and finding.evidence["inner"] == ["e2"]
    assert finding.confidence is Confidence.CANDIDATE


# -- With a catalog ---------------------------------------------------------


def catalog_driver(**answers):
    """A driver answering the catalog queries the diagnosis makes, by a fragment of each."""
    driver = MagicMock(spec=SqlDriver)
    calls: list[str] = []

    async def execute(query, *args, **kwargs):
        calls.append(query)
        for fragment, rows in answers.items():
            if fragment in query:
                return [SqlDriver.RowResult(cells=row) for row in rows]
        return []

    driver.execute_query = AsyncMock(side_effect=execute)
    driver.calls = calls
    return driver


POLICY = {
    "schema": "public",
    "table": "todos",
    "enabled": True,
    "forced": False,
    "policy": "todos_owner",
    "command": "r",
    "permissive": True,
    "roles": ["authenticated"],
    "qual": "(user_id = auth.uid())",
    "with_check": None,
}
UID = {
    "schema": "auth",
    "name": "uid",
    "volatility": "s",
    "leakproof": False,
    "language": "plpgsql",
    "security_definer": False,
    "has_config": False,
    "returns_set": False,
    "source": None,
}
COLUMNS = [{"schema": "public", "table": "todos", "column": c} for c in ("id", "user_id", "title", "done", "tags")]


@pytest.mark.asyncio
async def test_with_the_catalog_the_per_row_policy_call_replaces_the_index_advice():
    driver = catalog_driver(pg_policy=[POLICY], **{"FROM pg_proc p\nJOIN pg_namespace": [UID]})
    findings = await PlanDiagnoser(driver).diagnose_plan(plan("rls_per_row"))

    finding = only(findings, PlanIssue.RLS_PER_ROW_CALL)
    assert finding.confidence is Confidence.PROVEN
    assert finding.evidence["rows_tested"] == 200000
    assert finding.remediation == ['ALTER POLICY "todos_owner" ON "public"."todos" USING (user_id = (SELECT auth.uid()));']
    assert PlanIssue.DISCARDED_ROWS not in issues(findings)


@pytest.mark.asyncio
async def test_an_inlined_sql_function_is_matched_by_its_body_and_marked_a_candidate():
    """auth.uid_sql() is plain SQL, so the plan shows current_setting() where the call was."""
    policy = {**POLICY, "table": "notes", "policy": "notes_owner", "qual": "(user_id = auth.uid_sql())"}
    function = {
        **UID,
        "name": "uid_sql",
        "language": "sql",
        "source": "SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid",
    }
    driver = catalog_driver(pg_policy=[policy], **{"FROM pg_proc p\nJOIN pg_namespace": [function]})
    findings = await PlanDiagnoser(driver).diagnose_plan(plan("rls_inlined"))

    finding = only(findings, PlanIssue.RLS_PER_ROW_CALL)
    assert finding.confidence is Confidence.CANDIDATE
    assert finding.evidence["inlined"] is True
    assert "current_setting" in finding.detail


@pytest.mark.asyncio
async def test_a_policy_already_wrapped_is_left_alone():
    policy = {**POLICY, "qual": "(user_id = ( SELECT auth.uid() AS uid))"}
    driver = catalog_driver(pg_policy=[policy], **{"FROM pg_proc p\nJOIN pg_namespace": [UID]})
    findings = await PlanDiagnoser(driver).diagnose_plan(plan("rls_per_row"))
    assert PlanIssue.RLS_PER_ROW_CALL not in issues(findings)


@pytest.mark.asyncio
async def test_a_leaky_operator_with_an_index_under_the_policy_is_named():
    driver = catalog_driver(
        pg_policy=[POLICY],
        pg_operator=[{"name": "@>", "all_leakproof": False, "any_leakproof": False, "all_immutable": True, "any_immutable": True}],
        attisdropped=COLUMNS,
        pg_am=[{"schema": "public", "table": "todos", "index": "todos_tags_gin", "method": "gin", "partial": False, "keys": ["tags"]}],
        **{"FROM pg_proc p\nJOIN pg_namespace": [UID]},
    )
    findings = await PlanDiagnoser(driver).diagnose_plan(plan("rls_leaky"))

    finding = only(findings, PlanIssue.RLS_LEAKY_PREDICATE)
    assert finding.evidence["not_leakproof"] == ["@>"]
    assert finding.evidence["indexes"] == ["todos_tags_gin"]
    assert PlanIssue.RLS_PER_ROW_CALL in issues(findings)


@pytest.mark.asyncio
async def test_a_leaky_operator_with_no_index_for_it_is_not_reported():
    """Leakproofness only costs anything when there is an index it keeps the condition from."""
    driver = catalog_driver(
        pg_policy=[POLICY],
        pg_operator=[{"name": "@>", "all_leakproof": False, "any_leakproof": False, "all_immutable": True, "any_immutable": True}],
        attisdropped=COLUMNS,
        **{"FROM pg_proc p\nJOIN pg_namespace": [UID]},
    )
    findings = await PlanDiagnoser(driver).diagnose_plan(plan("rls_leaky"))
    assert PlanIssue.RLS_LEAKY_PREDICATE not in issues(findings)


@pytest.mark.asyncio
async def test_a_foreign_key_trigger_with_no_index_names_the_index():
    driver = catalog_driver(
        pg_constraint=[
            {
                "constraint": "children_parent_id_fkey",
                "referencing": "public.children",
                "referenced": "public.parents",
                "columns": ["parent_id"],
                "indexed": False,
            }
        ]
    )
    finding = only(await PlanDiagnoser(driver).diagnose_plan(plan("triggers")), PlanIssue.TRIGGER_TIME)
    assert finding.remediation == ['CREATE INDEX CONCURRENTLY ON "public"."children" (parent_id);']
    assert finding.confidence is Confidence.PROVEN


@pytest.mark.asyncio
async def test_a_function_on_an_indexed_column_suggests_an_expression_index_only_when_immutable():
    users = [{"schema": "public", "table": "users", "column": c} for c in ("id", "email", "status", "created_at")]
    index = [{"schema": "public", "table": "users", "index": "users_email_idx", "method": "btree", "partial": False, "keys": ["email"]}]
    lower = {**UID, "schema": "pg_catalog", "name": "lower", "volatility": "i", "language": "internal"}

    driver = catalog_driver(attisdropped=users, pg_am=index, **{"FROM pg_proc p\nJOIN pg_namespace": [lower]})
    findings = await PlanDiagnoser(driver).diagnose_plan(plan("expression_filter"))
    finding = only(findings, PlanIssue.FUNCTION_ON_INDEXED_COLUMN)
    assert finding.remediation == ['CREATE INDEX CONCURRENTLY ON "public"."users" ((lower(email)));']
    assert PlanIssue.DISCARDED_ROWS not in issues(findings)

    stable = {**lower, "volatility": "s"}
    driver = catalog_driver(attisdropped=users, pg_am=index, **{"FROM pg_proc p\nJOIN pg_namespace": [stable]})
    finding = only(await PlanDiagnoser(driver).diagnose_plan(plan("expression_filter")), PlanIssue.FUNCTION_ON_INDEXED_COLUMN)
    assert finding.remediation == []
    assert "not IMMUTABLE" in finding.detail


@pytest.mark.asyncio
async def test_with_a_connection_the_plan_control_advice_comes_from_the_server():
    driver = catalog_driver(hint_plan_installed=[{"hint_plan_installed": False, "hint_plan_available": True, "hint_plan_loaded": False}])

    async def aurora():
        return PlatformDetection(Platform.AURORA, "the server has the function aurora_version()", Basis.SERVER)

    async def version(_driver):
        return 16

    import pg_debug.plans.control as control

    original = control.get_postgres_version
    control.get_postgres_version = version
    try:
        findings = await PlanDiagnoser(driver, platform=aurora).diagnose_plan(plan("order_by_limit"))
    finally:
        control.get_postgres_version = original

    advice = only(findings, PlanIssue.LIMIT_SCAN_FILTER).plan_control
    assert advice[0].startswith("pg_hint_plan is available here but not installed")
    assert "in the parameter group" in advice[0]
    assert advice[1] == "This is Aurora, and Aurora Query Plan Management (apg_plan_mgmt) is not available to this server."


@pytest.mark.asyncio
async def test_plan_control_is_not_read_when_no_finding_concerns_a_planner_choice():
    driver = catalog_driver()
    await PlanDiagnoser(driver).diagnose_plan(plan("jit"))
    assert not any("pg_hint_plan" in query for query in driver.calls)
