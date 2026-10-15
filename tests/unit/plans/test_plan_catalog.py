"""Reading policies and rendered conditions, and naming what can pin a plan.

All pure: the text a plan or ``pg_get_expr`` rendered, and the facts the
catalog would return, with no server behind them.
"""

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.plans import catalog
from pg_debug.plans.catalog import FunctionFacts
from pg_debug.plans.catalog import calls_in
from pg_debug.plans.catalog import conjuncts
from pg_debug.plans.catalog import hoist_calls
from pg_debug.plans.catalog import normalized
from pg_debug.plans.catalog import operators_in
from pg_debug.plans.control import PLAN_CONTROL_SQL
from pg_debug.plans.control import PlanControl
from pg_debug.plans.control import plan_control_advice
from pg_debug.plans.diagnoser import SETTINGS_SQL
from pg_debug.platform import Basis
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection

UID = FunctionFacts("auth", "uid", "stable", False, "plpgsql", False)
RANDOM = FunctionFacts("pg_catalog", "random", "volatile", False, "internal", False)
MEMBER = FunctionFacts("public", "is_member", "stable", False, "sql", True)


# -- The rewrite ------------------------------------------------------------


def test_a_bare_stable_call_is_wrapped_so_it_runs_once():
    calls, rewritten = hoist_calls("(user_id = auth.uid())", [UID])
    assert [call.text for call in calls] == ["auth.uid()"]
    assert rewritten == "user_id = (SELECT auth.uid())"


def test_a_call_already_in_a_subquery_is_left_as_it_is():
    assert hoist_calls("(user_id = ( SELECT auth.uid() AS uid))", [UID]) == ([], None)


def test_a_call_on_the_rows_columns_stays_but_its_own_constant_arguments_are_wrapped():
    """is_member(team_id, …) depends on the row and cannot be made once; the
    auth.uid() inside it does not, and can."""
    calls, rewritten = hoist_calls("is_member(team_id, auth.uid())", [UID, MEMBER])
    assert [call.text for call in calls] == ["auth.uid()"]
    assert rewritten == "is_member(team_id, (SELECT auth.uid()))"


def test_a_volatile_call_is_not_wrapped_because_once_would_change_its_result():
    assert hoist_calls("(random() < 0.5)", [RANDOM]) == ([], None)


def test_an_unqualified_name_whose_overloads_disagree_is_not_resolved():
    other = FunctionFacts("other", "uid", "volatile", False, "sql", True)
    assert hoist_calls("(user_id = uid())", [UID, other]) == ([], None)


def test_a_repeated_call_is_listed_once():
    calls, _ = hoist_calls("((owner = auth.uid()) OR (editor = auth.uid()))", [UID])
    assert len(calls) == 1


def test_an_expression_that_does_not_parse_rewrites_to_nothing():
    assert hoist_calls("(user_id = ", [UID]) == ([], None)


# -- Rendered conditions ------------------------------------------------------


def test_calls_are_found_with_their_arguments_and_literals_are_not_read_into():
    found = calls_in("(lower((users.email)::text) = 'f(x)'::text)")
    assert [(c.name, c.arguments) for c in found] == [("lower", "(users.email)::text")]


def test_nullif_and_the_like_are_not_taken_for_functions_but_what_they_hold_is():
    found = calls_in("(t.user_id = (NULLIF(current_setting('a'::text, true), ''::text))::uuid)")
    assert [c.name for c in found] == ["current_setting"]


def test_conjuncts_split_only_at_the_top_level():
    assert conjuncts("((NOT t.done) AND ((t.a = 1) OR (t.b = 2)))") == ["(NOT t.done)", "((t.a = 1) OR (t.b = 2))"]
    assert conjuncts("(t.title = ' AND ')") == ["t.title = ' AND '"]


def test_operators_are_read_outside_literals():
    assert operators_in("""((t.tags @> '{"a": "<>"}'::jsonb) AND (t.n <> 1))""") == ["@>", "<>"]


def test_a_policy_and_the_plans_rendering_of_it_compare_equal():
    assert normalized("(user_id = auth.uid())") == normalized("(todos.user_id = auth.uid())")
    assert normalized("(user_id = auth.uid())") != normalized("(owner_id = auth.uid())")


# -- Restricted mode ----------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [catalog.POLICIES_SQL, catalog.FUNCTIONS_SQL, catalog.OPERATORS_SQL, catalog.INDEXES_SQL, catalog.FOREIGN_KEYS_SQL],
)
def test_every_catalog_read_passes_restricted_mode(query):
    SafeSqlDriver.__new__(SafeSqlDriver)._validate(SafeSqlDriver.param_sql_to_query(query, [["public.todos"]]))


@pytest.mark.parametrize("query", [PLAN_CONTROL_SQL, SETTINGS_SQL])
def test_the_plan_control_and_settings_reads_pass_restricted_mode(query):
    SafeSqlDriver.__new__(SafeSqlDriver)._validate(query)


# -- Plan control -------------------------------------------------------------


def detected(platform: Platform) -> PlatformDetection:
    return PlatformDetection(platform, "a test", Basis.SERVER)


def test_an_installed_but_unloaded_pg_hint_plan_says_it_needs_loading():
    (line,) = plan_control_advice(PlanControl(version=16, hint_plan_installed=True))
    assert line.startswith("pg_hint_plan is installed")
    assert "only takes effect once loaded" in line


def test_a_loaded_pg_hint_plan_needs_nothing_more():
    (line,) = plan_control_advice(PlanControl(version=16, hint_plan_installed=True, hint_plan_loaded=True))
    assert "once loaded" not in line


def test_postgresql_19_names_pg_plan_advice_and_that_it_was_in_beta():
    (line,) = plan_control_advice(PlanControl(version=19))
    assert "pg_plan_advice" in line and "beta as of October 2026" in line


def test_before_19_pg_plan_advice_is_not_offered():
    assert not any("pg_plan_advice" in line for line in plan_control_advice(PlanControl(version=18)))


def test_aurora_names_query_plan_management_as_installed_or_available():
    (installed,) = plan_control_advice(PlanControl(version=16, platform=detected(Platform.AURORA), apg_plan_mgmt_installed=True))
    assert "apg_plan_mgmt) is installed" in installed
    (available,) = plan_control_advice(PlanControl(version=16, platform=detected(Platform.AURORA), apg_plan_mgmt_available=True))
    assert "rds.enable_plan_management" in available


def test_alloydb_names_its_plan_management_without_claiming_to_have_checked_it():
    (line,) = plan_control_advice(PlanControl(version=16, platform=detected(Platform.ALLOYDB)))
    assert "AlloyDB" in line and "does not detect" in line


def test_a_server_with_nothing_says_so_rather_than_staying_silent():
    (line,) = plan_control_advice(PlanControl(version=16))
    assert line.startswith("Nothing here pins a plan")
