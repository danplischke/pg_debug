"""The arithmetic every verdict rests on.

These are the sums that are easy to get quietly wrong: time in a plan is per
loop and includes children, so the naive reading makes the root node 100% of
the query and says nothing. And a misestimate propagates upwards, so the naive
reading reports the cause together with a list of its own consequences.
"""

from pg_debug.artifacts import PlanNode
from pg_debug.plan.metrics import estimate_ratio
from pg_debug.plan.metrics import inclusive_ms
from pg_debug.plan.metrics import is_misestimated
from pg_debug.plan.metrics import lowest_misestimates
from pg_debug.plan.metrics import qualified_relation
from pg_debug.plan.metrics import self_ms
from pg_debug.plan.metrics import shares
from pg_debug.plan.metrics import total_self_ms
from pg_debug.plan.metrics import walk
from pg_debug.plan.metrics import walk_with_parent


def node(node_type="Seq Scan", plan_rows=100, actual_rows=100, ms=None, loops=1, children=(), **kwargs):
    """A plan node with only the fields a test cares about."""
    built = PlanNode(
        node_type=node_type,
        total_cost=1.0,
        startup_cost=0.0,
        plan_rows=plan_rows,
        plan_width=8,
        children=list(children),
        **kwargs,
    )
    built.actual_rows = actual_rows
    built.actual_loops = loops
    built.actual_total_time = ms
    return built


# -- Time ------------------------------------------------------------------


def test_a_nodes_own_time_excludes_its_children():
    """The root of a plan always contains the whole plan's time. Reporting that
    as the root's cost would make every diagnosis point at the root."""
    child = node(ms=80.0)
    parent = node("Aggregate", ms=100.0, children=[child])

    assert self_ms(parent) == 20.0
    assert self_ms(child) == 80.0


def test_time_is_multiplied_by_the_loops_the_node_ran():
    """``Actual Total Time`` is one execution. A node run 500 times at 2ms cost
    a second, and reading it as 2ms is the single biggest way to miss the
    expensive part of a plan."""
    assert inclusive_ms(node(ms=2.0, loops=500)) == 1000.0


def test_a_node_that_would_come_out_negative_is_clamped_to_zero():
    """A Gather's children report the summed work of several processes while
    the Gather reports wall clock, so the subtraction goes negative. The node
    did not take less than no time."""
    workers = node(ms=100.0, loops=3)
    gather = node("Gather", ms=110.0, children=[workers])

    assert self_ms(gather) == 0.0


def test_a_plan_with_no_analyze_has_no_time_rather_than_wrong_time():
    assert self_ms(node(ms=None)) == 0.0
    assert total_self_ms(node(ms=None)) == 0.0


def test_shares_are_taken_against_the_trees_own_total():
    child = node(ms=75.0)
    root = node("Aggregate", ms=100.0, children=[child])

    result = shares(root)
    assert result[id(child)] == 0.75
    assert result[id(root)] == 0.25
    assert sum(result.values()) == 1.0


def test_shares_are_zero_rather_than_undefined_on_an_untimed_plan():
    root = node(ms=None)
    assert shares(root) == {id(root): 0.0}


# -- Estimates -------------------------------------------------------------


def test_the_ratio_is_symmetric_between_over_and_underestimating():
    """Both mislead the planner, so both count. An overestimate is what makes
    it hash a table it could have looped over."""
    assert estimate_ratio(node(plan_rows=10, actual_rows=1000)) == 100.0
    assert estimate_ratio(node(plan_rows=1000, actual_rows=10)) == 100.0


def test_finding_no_rows_where_some_were_expected_does_not_divide_by_zero():
    assert estimate_ratio(node(plan_rows=500, actual_rows=0)) == 500.0


def test_a_plan_with_no_analyze_reports_no_divergence():
    """There is nothing to compare an estimate against, which is not the same
    as the estimate being right."""
    plain = node(plan_rows=10)
    plain.actual_rows = None

    assert estimate_ratio(plain) == 1.0
    assert is_misestimated(plain, min_ratio=2) is False


def test_a_large_ratio_over_a_handful_of_rows_is_not_reported():
    """One row estimated where twenty were found is a factor of twenty and
    costs nothing. A plan is full of those, and reporting them buries the one
    that matters."""
    assert is_misestimated(node(plan_rows=1, actual_rows=20), min_ratio=10) is False


def test_a_large_ratio_over_many_rows_is_reported():
    assert is_misestimated(node(plan_rows=17, actual_rows=4000), min_ratio=10) is True


# -- Which node to blame ---------------------------------------------------


def test_only_the_lowest_diverging_node_is_reported():
    """A wrong scan estimate makes every join above it wrong too. Those are
    consequences, and listing them alongside the cause is what makes plan
    tooling unreadable."""
    scan = node("Seq Scan", plan_rows=17, actual_rows=4000)
    join = node("Nested Loop", plan_rows=20, actual_rows=5000, children=[scan])
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[join])

    assert [id(n) for n in lowest_misestimates(root, min_ratio=10)] == [id(scan)]


def test_two_independent_causes_are_both_reported():
    """Different branches of the plan are not each other's consequences."""
    left = node("Seq Scan", plan_rows=10, actual_rows=5000)
    right = node("Seq Scan", plan_rows=8000, actual_rows=200)
    root = node("Hash Join", plan_rows=100, actual_rows=100, children=[left, right])

    assert [id(n) for n in lowest_misestimates(root, min_ratio=10)] == [id(left), id(right)]


def test_a_plan_whose_estimates_are_right_yields_nothing():
    """The direction that matters more: a rule that fires on a good plan is
    worse than no rule."""
    scan = node("Index Scan", plan_rows=1000, actual_rows=1010)
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])

    assert lowest_misestimates(root, min_ratio=10) == []


# -- Walking ---------------------------------------------------------------


def test_the_walk_reaches_every_node():
    leaf = node("Seq Scan")
    root = node("Sort", children=[node("Hash Join", children=[leaf])])

    visited = list(walk(root))
    assert len(visited) == 3
    assert any(seen is leaf for seen in visited)


def test_walking_with_parents_pairs_the_root_with_nothing():
    leaf = node("Seq Scan")
    root = node("Sort", children=[leaf])

    # Keyed by id: PlanNode carries attrs-generated equality and so is
    # unhashable, which is why the diagnoser keys its parent map the same way.
    parents = {id(child): parent for child, parent in walk_with_parent(root)}
    assert parents[id(root)] is None
    assert parents[id(leaf)] is root


# -- Naming ----------------------------------------------------------------


def test_a_relation_is_qualified_when_the_plan_says_which_schema():
    assert qualified_relation(node(relation_name="visits", schema_name="plandemo")) == "plandemo.visits"


def test_a_relation_without_a_schema_is_left_as_the_plan_wrote_it():
    """Inventing ``public.`` would be a guess, and the wrong one whenever a
    search_path put something else first."""
    assert qualified_relation(node(relation_name="visits")) == "visits"


def test_a_node_that_reads_no_table_has_no_relation():
    assert qualified_relation(node("Sort")) is None
