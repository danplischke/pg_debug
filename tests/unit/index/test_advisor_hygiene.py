"""What the advisor checks before it recommends: regressions, merges, unread indexes, and what each index serves.

The planner is stubbed: each query's cost under each set of hypothetical
indexes comes from a table, and its plan names the indexes the table says it
reads, as HypoPG would name them.
"""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pglast import parse_sql

from pg_debug.db import SqlDriver
from pg_debug.db.window import CountingWindow
from pg_debug.errors import InvalidArgumentError
from pg_debug.index.base import DEFAULT_MAX_REGRESSION_FACTOR
from pg_debug.index.base import IndexRecommendation
from pg_debug.index.base import IndexTuningBase
from pg_debug.index.base import IndexTuningResult
from pg_debug.index.base import _merged_columns
from pg_debug.index.base import is_hypothetical_name_of
from pg_debug.index.presentation import TextPresentation
from pg_debug.plans.hypothetical import IndexDefinition

A = IndexRecommendation("t", ("a",))
AB = IndexRecommendation("t", ("a", "b"))
AC = IndexRecommendation("t", ("a", "c"))
ABC = IndexRecommendation("t", ("a", "b", "c"))
D = IndexRecommendation("t", ("d",))

Q1 = "select * from t where a = 1"
Q2 = "select * from t where a = 1 and b = 2"
Q3 = "select * from t where d = 3"


def hypo(index: IndexRecommendation, oid: int = 1) -> str:
    return f"<{oid}>btree_{index.table}_{'_'.join(index.columns)}"


class StubAdvisor(IndexTuningBase):
    """Plans from a table: query -> {frozenset of column tuples -> (cost, index read)}."""

    def __init__(self, plans: dict[str, Any], default: dict[str, float], **kwargs: Any):
        driver = MagicMock(spec=SqlDriver)
        driver.execute_query = AsyncMock(return_value=[])
        super().__init__(driver, **kwargs)
        self.budget_mb = -1
        self.plans = plans
        self.default = default

    async def get_explain_plan_with_indexes(self, query_text: str, indexes: frozenset[IndexDefinition]) -> dict[str, Any]:
        key = frozenset(idx.columns for idx in indexes)
        cost, reads = self.plans.get(query_text, {}).get(key, (self.default[query_text], None))
        node: dict[str, Any] = {"Node Type": "Seq Scan", "Total Cost": cost}
        if reads is not None:
            node = {"Node Type": "Bitmap Heap Scan", "Total Cost": cost, "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": hypo(reads)}]}
        return {"Plan": node}

    async def write_load(self, table: str) -> dict[str, float]:
        return {}

    async def _index_size(self, index: IndexRecommendation) -> int:
        return 1000 * len(index.columns)

    async def _generate_recommendations(self, query_weights):  # pragma: no cover - not searched here
        raise NotImplementedError


def weights(*queries: str) -> list[tuple[str, Any, float]]:
    return [(q, parse_sql(q)[0].stmt, 1.0) for q in queries]


def cols(*indexes: IndexRecommendation) -> frozenset[tuple[str, ...]]:
    return frozenset(idx.columns for idx in indexes)


# -- HypoPG's names ------------------------------------------------------------


def test_a_plan_names_a_hypothetical_index_by_method_table_and_columns():
    index = IndexDefinition("public.orders", ("customer_id", "status"))
    assert is_hypothetical_name_of("<13605>btree_orders_customer_id_status", index)
    assert not is_hypothetical_name_of("<13605>btree_orders_customer_id", index)
    assert not is_hypothetical_name_of("orders_customer_id_status_idx", index)


def test_a_name_cut_at_63_bytes_matches_as_a_prefix_only_then():
    index = IndexDefinition("t", ("a_very_long_column_name_one", "a_very_long_column_name_two"))
    full = f"<13605>btree_t_{'_'.join(index.columns)}"
    cut = full.encode()[:63].decode()
    assert is_hypothetical_name_of(cut, index)
    # A shorter name is a whole name, and a prefix of it is another index.
    assert not is_hypothetical_name_of("<13605>btree_t_a_very", index)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (("a",), ("a", "b"), [("a", "b")]),
        (("a", "b"), ("a",), [("a", "b")]),
        (("a", "b"), ("a", "c"), [("a", "b", "c"), ("a", "c", "b")]),
    ],
)
def test_the_merged_index_is_the_wider_or_one_followed_by_the_rest_of_the_other(first, second, expected):
    assert _merged_columns(first, second) == expected


def test_a_negative_regression_factor_is_refused():
    with pytest.raises(InvalidArgumentError, match="max_regression_factor"):
        StubAdvisor({}, {}, max_regression_factor=-0.1)


# -- The regression bound ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_index_that_raises_a_query_past_the_bound_is_refused_with_the_figure():
    """The workload's total improves with both indexes, which is what the
    search optimises; Q3 alone costs 1.5 times its baseline with D in the set."""
    plans = {
        Q1: {cols(A): (10.0, A), cols(A, D): (10.0, A)},
        Q3: {cols(D): (150.0, D), cols(A, D): (150.0, D)},
    }
    advisor = StubAdvisor(plans, {Q1: 1000.0, Q3: 100.0})

    result = await advisor._format_recommendations(weights(Q1, Q3), ({A, D}, 0.0))

    assert [r.columns for r in result] == [("a",)]
    check = advisor._regression_check
    assert check is not None
    assert check["max_regression_factor"] == DEFAULT_MAX_REGRESSION_FACTOR
    assert check["queries_checked"] == 2
    [refused] = check["refused"]
    assert refused["columns"] == ["d"]
    assert refused["queries"] == [{"query": Q3, "baseline_cost": 100.0, "cost_with_set": 150.0, "ratio": 1.5}]
    assert check["worst_query"]["ratio"] <= 1.0


@pytest.mark.asyncio
async def test_a_rise_within_the_bound_is_kept_and_reported():
    plans = {Q1: {cols(A): (10.0, A)}, Q3: {cols(A): (105.0, None)}}
    advisor = StubAdvisor(plans, {Q1: 1000.0, Q3: 100.0})

    result = await advisor._format_recommendations(weights(Q1, Q3), ({A}, 0.0))

    assert [r.columns for r in result] == [("a",)]
    assert advisor._regression_check is not None
    assert advisor._regression_check["refused"] == []
    assert advisor._regression_check["worst_query"] == {"query": Q3, "baseline_cost": 100.0, "cost_with_set": 105.0, "ratio": 1.05}


@pytest.mark.asyncio
async def test_a_factor_of_zero_refuses_any_rise():
    plans = {Q1: {cols(A): (10.0, A)}, Q3: {cols(A): (100.5, None)}}
    advisor = StubAdvisor(plans, {Q1: 1000.0, Q3: 100.0}, max_regression_factor=0.0)

    assert await advisor._format_recommendations(weights(Q1, Q3), ({A}, 0.0)) == []
    assert advisor._regression_check is not None
    assert advisor._regression_check["refused"][0]["queries"][0]["ratio"] == 1.005


# -- Merging -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_index_whose_columns_lead_another_is_merged_into_it():
    plans = {
        Q1: {cols(A): (10.0, A), cols(AB): (10.5, AB), cols(A, AB): (10.0, A)},
        Q2: {cols(A): (50.0, A), cols(AB): (5.0, AB), cols(A, AB): (5.0, AB)},
    }
    advisor = StubAdvisor(plans, {Q1: 1000.0, Q2: 1000.0})

    result = await advisor._format_recommendations(weights(Q1, Q2), ({A, AB}, 0.0))

    assert [r.columns for r in result] == [("a", "b")]
    assert result[0].queries_using == [Q1, Q2]
    [merge] = advisor._consolidations
    assert merge["merged"] is True
    assert merge["into"] == AB.definition
    assert merge["worst_ratio"] == 1.05
    assert (merge["size_before_bytes"], merge["size_after_bytes"]) == (3000, 2000)


@pytest.mark.asyncio
async def test_a_merge_that_raises_a_query_past_the_bound_is_refused_with_the_figure():
    q_ac = "select * from t where a = 1 and c = 3"
    plans = {
        Q2: {cols(AB): (5.0, AB), cols(AC): (40.0, AC), cols(AB, AC): (5.0, AB), cols(ABC, AC): (5.0, ABC), cols(AB, ABC): (5.0, AB)},
        q_ac: {cols(AC): (5.0, AC), cols(AB): (40.0, AB), cols(AB, AC): (5.0, AC), cols(ABC): (8.0, ABC)},
    }
    default = {Q2: 1000.0, q_ac: 1000.0}
    advisor = StubAdvisor(plans, default)
    # Merged either way round, the other query costs more than 1.1 times what it did.
    advisor.plans[Q2][cols(IndexRecommendation("t", ("a", "c", "b")))] = (6.0, None)
    advisor.plans[Q2][cols(ABC)] = (5.0, ABC)

    result = await advisor._format_recommendations(weights(Q2, q_ac), ({AB, AC}, 0.0))

    assert sorted(r.columns for r in result) == [("a", "b"), ("a", "c")]
    [refusal] = advisor._consolidations
    assert refusal["merged"] is False
    # The closer of the two merges: (a, b, c), with which the second query costs 8.0 instead of 5.0.
    assert refusal["tried"] == ABC.definition
    assert refusal["ratio"] == 1.6
    assert (refusal["query"], refusal["cost_with_both"], refusal["cost_with_merged"]) == (q_ac, 5.0, 8.0)
    assert "over the bound of 1.10" in refusal["reason"]


@pytest.mark.asyncio
async def test_a_merge_wider_than_allowed_is_refused_and_says_so():
    advisor = StubAdvisor({Q2: {cols(AB): (5.0, AB), cols(AC): (5.0, AC), cols(AB, AC): (5.0, AB)}}, {Q2: 1000.0})
    advisor.max_index_width = 2

    await advisor._format_recommendations(weights(Q2), ({AB, AC}, 0.0))

    [refusal] = advisor._consolidations
    assert refusal["merged"] is False
    assert "more than the 2 allowed" in refusal["reason"]


# -- What each index serves ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_index_no_plan_reads_is_refused_once_the_planner_agrees():
    plans = {
        Q1: {cols(A): (10.0, A), cols(A, D): (10.0, A)},
        Q3: {cols(D): (5.0, D), cols(A, D): (5.0, D)},
        Q2: {cols(A): (20.0, A), cols(A, D): (20.0, A)},
    }
    # D is read by Q3; A by Q1 and Q2. Add an index nothing reads.
    plans[Q1][cols(A, D, AC)] = (10.0, A)
    plans[Q2][cols(A, D, AC)] = (20.0, A)
    plans[Q3][cols(A, D, AC)] = (5.0, D)
    advisor = StubAdvisor(plans, {Q1: 1000.0, Q2: 1000.0, Q3: 1000.0})
    advisor._consolidate = AsyncMock(side_effect=lambda _w, indexes: indexes)  # keep AC beside A

    result = await advisor._format_recommendations(weights(Q1, Q2, Q3), ({A, D, AC}, 0.0))

    assert {r.columns: r.queries_using for r in result} == {("a",): [Q1, Q2], ("d",): [Q3]}
    [refused] = advisor._refused_candidates
    assert refused["indexes"] == [AC.definition]
    assert "no query's plan reads it" in refused["reason"]


@pytest.mark.asyncio
async def test_an_index_whose_name_did_not_match_but_matters_is_kept():
    """A name the matching misses must not cost the user an index the planner uses."""
    plans = {Q1: {cols(A): (10.0, None)}}  # read, but under a name that does not match
    advisor = StubAdvisor(plans, {Q1: 1000.0})

    result = await advisor._format_recommendations(weights(Q1), ({A}, 0.0))

    assert [r.columns for r in result] == [("a",)]
    assert result[0].queries_using == []
    assert advisor._refused_candidates == []


@pytest.mark.asyncio
async def test_the_order_is_by_benefit_on_its_own_whatever_the_set_iterates():
    plans = {
        Q1: {cols(A): (10.0, A), cols(A, D): (10.0, A)},
        Q3: {cols(D): (1.0, D), cols(A, D): (1.0, D)},
    }
    advisor = StubAdvisor(plans, {Q1: 100.0, Q3: 1000.0})

    result = await advisor._format_recommendations(weights(Q1, Q3), ({A, D}, 0.0))

    assert [r.columns for r in result] == [("d",), ("a",)]


# -- The workload --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_workload_from_pg_stat_statements_reports_its_counting_window():
    advisor = StubAdvisor({}, {})
    advisor._statement_stats = MagicMock()
    window = CountingWindow(since=None, now=MagicMock(), began_with=None)
    with patch("pg_debug.index.base.statement_window", AsyncMock(return_value=window)):
        assert await advisor._workload_window() == window.to_dict()

    with patch("pg_debug.index.base.statement_window", AsyncMock(side_effect=RuntimeError("gone"))):
        assert await advisor._workload_window() is None


def test_the_result_says_what_varies_from_run_to_run():
    session = IndexTuningResult(session_id="1", budget_mb=-1, workload_source="query_store", workload=[{"query": "x"}])
    session.workload_window = {"counting_since": "2026-10-14T10:00:00+00:00", "counting_seconds": 3600, "counting_began_with": "reset"}
    session.regression_check = {"max_regression_factor": 0.1, "queries_checked": 1, "worst_query": None, "refused": []}

    checks = TextPresentation(MagicMock(), MagicMock())._checks(session)

    assert checks["regression_check"]["queries_checked"] == 1
    assert checks["workload"]["source"] == "query_store"
    assert checks["workload"]["queries"] == 1
    assert checks["workload"]["counting_window"]["counting_seconds"] == 3600
    assert "deterministic" in checks["workload"]["note"]
    assert "consolidations" not in checks and "refused_candidates" not in checks


# -- The same workload, the same candidates ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [["c", "a", "b"], ["b", "c", "a"]])
async def test_candidate_columns_come_in_one_order_whatever_the_set_iterates(order):
    """A set's order changes from one process to the next, and it used to set
    each candidate's column order, so two runs recommended (b, a) and (a, b)."""
    from pg_debug.index.dta import DatabaseTuningAdvisor

    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])
    advisor = DatabaseTuningAdvisor(driver)
    query = "select * from t where a = 1 and b = 2 and c = 3"

    class Ordered(set[str]):
        def __iter__(self):
            return iter(order)

    with (
        patch.object(advisor._sql_bind_params, "extract_stmt_columns", return_value={"t": Ordered(order)}),
        patch.object(advisor, "indexable_tables", AsyncMock(return_value={"t"})),
        patch.object(advisor, "_filter_long_text_columns", AsyncMock(side_effect=lambda c: c)),
        patch.object(advisor, "measure_index_sizes", AsyncMock()),
    ):
        candidates = await advisor.generate_candidates(weights(query), set())

    assert [c.columns for c in candidates] == [("a",), ("b",), ("c",), ("a", "b"), ("a", "c"), ("b", "c"), ("a", "b", "c")]
