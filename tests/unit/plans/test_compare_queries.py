"""compare-queries: values written into a statement, and runs compared."""

from types import SimpleNamespace

import pytest

from pg_debug.errors import ExplainPlanError
from pg_debug.errors import InvalidArgumentError
from pg_debug.plans.compare import bind
from pg_debug.plans.compare import compare_queries
from pg_debug.plans.compare import literal


@pytest.mark.parametrize(
    ("value", "sql"),
    [(42, "42"), (1.5, "1.5"), ("open", "'open'"), ("it's", "'it''s'"), (None, "NULL"), (True, "TRUE"), ([1, 2], "'[1, 2]'")],
)
def test_a_value_becomes_a_literal(value, sql):
    assert literal(value) == sql


def test_parameters_are_replaced_and_text_that_looks_like_one_is_not():
    sql = "SELECT * FROM t WHERE a = $2 AND b = '$1' /* $1 */ AND c = $1 AND d = $10"
    values: list[object] = list(range(1, 11))
    values[0], values[1] = "x", 7
    assert bind(sql, values) == "SELECT * FROM t WHERE a = 7 AND b = '$1' /* $1 */ AND c = 'x' AND d = 10"


def test_too_few_values_or_values_for_no_parameter_are_refused():
    with pytest.raises(InvalidArgumentError, match=r"uses \$2"):
        bind("SELECT $1, $2", [1])
    with pytest.raises(InvalidArgumentError, match="no \\$1-style parameters"):
        bind("SELECT 1", [1])


class FakeDiagnoser:
    """Plays back a plan per statement: (execution times, rows, shape node)."""

    def __init__(self, plays):
        self.plays = plays
        self.ran: list[str] = []

    def ensure_can_run(self):
        pass

    async def run(self, sql):
        self.ran.append(sql)
        times, rows, node = self.plays[sql]
        if times is None:
            raise ExplainPlanError(f"could not run {sql}")
        ms = times[sum(1 for s in self.ran if s == sql) - 1]
        top = {"Node Type": node, "Actual Rows": rows, "Actual Loops": 1, "Shared Hit Blocks": 10, "Shared Read Blocks": 2}
        return {"Plan": top}, SimpleNamespace(execution_time=ms, planning_time=0.5)

    async def findings(self, plan, min_ratio=None):
        return []


@pytest.mark.asyncio
async def test_each_variant_runs_with_each_set_of_values_and_the_median_is_reported():
    plays = {
        "SELECT * FROM t WHERE k = 1": ([90.0, 40.0, 41.0], 5, "Seq Scan"),
        "SELECT * FROM t WHERE k = 2": ([1.0, 1.0, 1.0], 5, "Index Scan"),
        "SELECT * FROM t WHERE k + 0 = 1": ([50.0, 50.0, 50.0], 5, "Seq Scan"),
        "SELECT * FROM t WHERE k + 0 = 2": ([50.0, 50.0, 50.0], 5, "Seq Scan"),
    }
    comparison = await compare_queries(FakeDiagnoser(plays), ["SELECT * FROM t WHERE k = $1", "SELECT * FROM t WHERE k + 0 = $1"], [[1], [2]])

    assert [(run.label, run.execution_ms) for run in comparison.runs] == [("A (1)", 41.0), ("A (2)", 1.0), ("B (1)", 50.0), ("B (2)", 50.0)]
    assert comparison.fastest == "A (2)"
    assert (comparison.runs[0].shared_hit, comparison.runs[0].shared_read) == (10, 2)
    notes = " ".join(comparison.notes)
    assert "Variant A got 2 different plans" in notes
    assert "Variant B" not in notes
    assert "first run of A (1)" in notes
    assert "different numbers of rows" not in notes


@pytest.mark.asyncio
async def test_variants_that_return_different_rows_are_said_not_to_be_the_same_query():
    plays = {"SELECT a": ([1.0], 10, "Seq Scan"), "SELECT b": ([0.5], 3, "Seq Scan")}
    comparison = await compare_queries(FakeDiagnoser(plays), ["SELECT a", "SELECT b"], repeat=1)

    notes = " ".join(comparison.notes)
    assert "different numbers of rows (A 10, B 3)" in notes
    assert "ran once" in notes


@pytest.mark.asyncio
async def test_a_variant_that_fails_is_reported_and_the_rest_still_run():
    plays = {"SELECT broken": (None, 0, ""), "SELECT ok": ([2.0], 1, "Result")}
    comparison = await compare_queries(FakeDiagnoser(plays), ["SELECT broken", "SELECT ok"], repeat=1)

    assert "could not run" in (comparison.runs[0].error or "")
    assert comparison.fastest == "B"


@pytest.mark.asyncio
async def test_out_of_range_requests_are_refused_before_anything_runs():
    diagnoser = FakeDiagnoser({})
    with pytest.raises(InvalidArgumentError):
        await compare_queries(diagnoser, [], repeat=1)
    with pytest.raises(InvalidArgumentError):
        await compare_queries(diagnoser, ["SELECT 1"], repeat=0)
    with pytest.raises(InvalidArgumentError, match="limit is 50"):
        await compare_queries(diagnoser, ["SELECT $1"] * 6, [[i] for i in range(9)])
    with pytest.raises(InvalidArgumentError, match=r"uses \$2"):
        await compare_queries(diagnoser, ["SELECT $1", "SELECT $1, $2"], [[1]])
    assert diagnoser.ran == []
