"""Each rule firing on its target, and staying quiet on everything else.

The second half is the half that matters. A diagnosis is only worth reading if
an empty result means something, and a rule that fires on a healthy plan
destroys that. So most of these assert silence.
"""

import pytest

from pg_debug.artifacts import PlanNode
from pg_debug.plan.result import PlanIssue
from pg_debug.plan.result import severity_for_share
from pg_debug.plan.rules import RULES
from pg_debug.plan.rules import RuleContext
from pg_debug.plan.rules import _columns_in
from pg_debug.plan.rules import _statistics_name
from pg_debug.plan.rules import correlated_columns
from pg_debug.plan.rules import discarded_rows
from pg_debug.plan.rules import hash_spilled
from pg_debug.plan.rules import lossy_bitmap
from pg_debug.plan.rules import nested_loop_blowup
from pg_debug.plan.rules import row_estimate
from pg_debug.plan.rules import sort_spilled
from pg_debug.plan.rules import stale_statistics
from pg_debug.plan.rules import workers_not_launched
from pg_debug.severity import Severity


def node(node_type="Seq Scan", plan_rows=100, actual_rows=100, ms=10.0, loops=1, children=(), **kwargs):
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


def context(root, **kwargs):
    """A context over a real tree, so parents and totals are consistent with it."""
    from pg_debug.plan.metrics import total_self_ms
    from pg_debug.plan.metrics import walk_with_parent

    kwargs.setdefault("parents", {id(n): p for n, p in walk_with_parent(root)})
    kwargs.setdefault("total_ms", total_self_ms(root))
    kwargs.setdefault("min_ratio", 10.0)
    return RuleContext(**kwargs)


# -- Severity ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("share", "expected"),
    [(0.9, Severity.HIGH), (0.5, Severity.HIGH), (0.2, Severity.MEDIUM), (0.15, Severity.MEDIUM), (0.02, Severity.LOW)],
)
def test_severity_comes_from_the_share_of_work_behind_a_finding(share, expected):
    """Measured rather than asserted. A rule calling its own findings important
    is not evidence of anything."""
    assert severity_for_share(share) is expected


# -- row_estimate -----------------------------------------------------------


def test_a_diverging_estimate_is_reported_with_both_numbers():
    scan = node(plan_rows=17, actual_rows=4000, relation_name="visits", schema_name="s")
    root = node("Aggregate", plan_rows=1, actual_rows=1, ms=20.0, children=[scan])

    (finding,) = row_estimate(root, context(root))
    assert finding.issue is PlanIssue.ROW_ESTIMATE
    assert "17" in finding.summary and "4,000" in finding.summary
    assert "underestimate" in finding.summary
    assert finding.relation == "s.visits"


def test_an_overestimate_is_named_as_one():
    scan = node(plan_rows=8000, actual_rows=100, relation_name="visits", schema_name="s")
    root = node("Aggregate", plan_rows=1, actual_rows=1, ms=20.0, children=[scan])

    (finding,) = row_estimate(root, context(root))
    assert "overestimate" in finding.summary


def test_the_error_is_charged_for_the_whole_subtree_it_misled():
    """The scan itself is cheap; the join above it that chose a strategy from
    the wrong number is not. Charging the scan for its own 1ms would rank the
    finding as trivial when it is the reason the query is slow."""
    scan = node(plan_rows=17, actual_rows=4000, ms=1.0)
    join = node("Nested Loop", plan_rows=20, actual_rows=5000, ms=900.0, children=[scan])
    root = node("Aggregate", plan_rows=1, actual_rows=1, ms=901.0, children=[join])

    (finding,) = row_estimate(root, context(root))
    assert finding.cost_ms == 900.0
    assert finding.severity is Severity.HIGH
    assert "Nested Loop" in finding.detail


def test_a_plan_whose_estimates_are_right_produces_nothing():
    scan = node(plan_rows=1000, actual_rows=1010)
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])

    assert row_estimate(root, context(root)) == []


# -- stale_statistics -------------------------------------------------------


def test_a_misestimate_on_a_table_with_stale_statistics_says_so():
    scan = node(plan_rows=17, actual_rows=4000, relation_name="visits", schema_name="s")
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])
    stale = {"s.visits": {"changed_rows": 900000, "changed_fraction": 0.9, "last_analyzed": "2024-01-01", "never_analyzed": False}}

    (finding,) = stale_statistics(root, context(root, stale=stale))
    assert finding.remediation == ["ANALYZE s.visits;"]
    assert "900,000" in finding.detail


def test_a_table_that_was_never_analyzed_is_described_as_such():
    scan = node(plan_rows=17, actual_rows=4000, relation_name="visits", schema_name="s")
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])
    stale = {"s.visits": {"never_analyzed": True}}

    (finding,) = stale_statistics(root, context(root, stale=stale))
    assert "never been analyzed" in finding.detail


def test_a_misestimate_on_a_freshly_analyzed_table_is_not_blamed_on_statistics():
    scan = node(plan_rows=17, actual_rows=4000, relation_name="visits", schema_name="s")
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])

    assert stale_statistics(root, context(root, stale={})) == []


# -- correlated_columns -----------------------------------------------------


def scan_over(condition, plan_rows=17, actual_rows=4000):
    return node(plan_rows=plan_rows, actual_rows=actual_rows, relation_name="visits", schema_name="s", filter=condition)


def test_two_restricted_columns_with_no_extended_statistics_are_reported():
    scan = scan_over("((visits.country = 'c7'::text) AND (visits.city = 'city7'::text))")
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])

    (finding,) = correlated_columns(root, context(root, columns={"s.visits": {"country", "city", "id"}}))
    assert finding.evidence["columns"] == ["country", "city"]
    assert finding.remediation[0] == "CREATE STATISTICS s.visits_country_city_stx (dependencies, mcv) ON country, city FROM s.visits;"
    assert finding.remediation[1] == "ANALYZE s.visits;"


def test_columns_already_covered_by_a_statistics_object_are_left_alone():
    """If the estimate is still wrong with the statistics in place, the cause
    is something else and saying "add statistics" would be noise."""
    scan = scan_over("((visits.country = 'c7'::text) AND (visits.city = 'city7'::text))")
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])
    ctx = context(root, columns={"s.visits": {"country", "city"}}, covered={"s.visits": [{"country", "city"}]})

    assert correlated_columns(root, ctx) == []


def test_a_single_restricted_column_is_not_a_correlation():
    """Extended statistics describe a relationship between columns. There is no
    relationship in one column."""
    scan = scan_over("(visits.country = 'c7'::text)")
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])

    assert correlated_columns(root, context(root, columns={"s.visits": {"country", "city"}})) == []


def test_an_overestimate_is_not_blamed_on_correlation():
    """The independence assumption multiplies selectivities, which makes an
    estimate too small. Too large has some other cause and CREATE STATISTICS
    would not touch it."""
    scan = scan_over("((visits.country = 'c7'::text) AND (visits.city = 'city7'::text))", plan_rows=8000, actual_rows=100)
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])

    assert correlated_columns(root, context(root, columns={"s.visits": {"country", "city"}})) == []


def test_an_or_between_the_columns_is_not_the_independence_assumption():
    scan = scan_over("((visits.country = 'c7'::text) OR (visits.city = 'city7'::text))")
    root = node("Aggregate", plan_rows=1, actual_rows=1, children=[scan])

    assert correlated_columns(root, context(root, columns={"s.visits": {"country", "city"}})) == []


def test_a_generated_statistics_name_stays_within_the_identifier_limit():
    long_columns = ["column_with_quite_a_long_name_" + str(i) for i in range(5)]
    schema, _, name = _statistics_name("s.a_table", long_columns).partition(".")

    assert schema == "s"
    assert len(name) <= 63


def test_a_generated_statistics_name_is_put_in_the_tables_own_schema():
    """An unqualified CREATE STATISTICS lands in whatever the search_path puts
    first, which is not where the table is and collides with the same table
    name in another schema."""
    assert _statistics_name("reporting.visits", ["a", "b"]) == "reporting.visits_a_b_stx"


# -- Reading a rendered condition -------------------------------------------


def test_only_the_tables_real_columns_are_taken_from_a_condition():
    """PostgreSQL renders a condition for display, not for a parser: it is full
    of casts, literals and qualifiers. Intersecting with the catalog is what
    keeps a type name from being read as a column."""
    condition = "((visits.country = 'c7'::text) AND (visits.city = 'city7'::text))"
    assert _columns_in(condition, {"country", "city", "id"}) == ["country", "city"]


def test_a_literal_that_looks_like_a_column_name_is_not_one():
    assert _columns_in("(visits.country = 'city'::text)", {"country", "city"}) == ["country"]


def test_a_cast_to_a_type_sharing_a_columns_name_is_not_a_column():
    assert _columns_in("((v.name)::text = 'x'::text)", {"name", "text"}) == ["name"]


# -- nested_loop_blowup -----------------------------------------------------


def test_a_loop_running_far_more_often_than_planned_is_reported():
    outer = node(plan_rows=3, actual_rows=5000, ms=1.0, parent_relationship="Outer")
    inner = node("Index Scan", plan_rows=1, actual_rows=1, ms=0.2, loops=5000, parent_relationship="Inner")
    loop = node("Nested Loop", plan_rows=3, actual_rows=5000, ms=1100.0, children=[outer, inner])

    (finding,) = nested_loop_blowup(loop, context(loop))
    assert finding.evidence == {"planned_iterations": 3, "actual_iterations": 5000, "outer_node": "Seq Scan", "inner_node": "Index Scan"}


def test_a_loop_that_ran_about_as_often_as_planned_is_left_alone():
    outer = node(plan_rows=4800, actual_rows=5000, ms=1.0, parent_relationship="Outer")
    inner = node("Index Scan", plan_rows=1, actual_rows=1, ms=0.2, loops=5000, parent_relationship="Inner")
    loop = node("Nested Loop", plan_rows=4800, actual_rows=5000, ms=1100.0, children=[outer, inner])

    assert nested_loop_blowup(loop, context(loop)) == []


def test_a_handful_of_extra_iterations_is_not_a_blowup():
    outer = node(plan_rows=1, actual_rows=40, ms=1.0, parent_relationship="Outer")
    inner = node("Index Scan", plan_rows=1, actual_rows=1, ms=0.2, loops=40, parent_relationship="Inner")
    loop = node("Nested Loop", plan_rows=1, actual_rows=40, ms=20.0, children=[outer, inner])

    assert nested_loop_blowup(loop, context(loop)) == []


# -- discarded_rows ---------------------------------------------------------


def test_a_scan_that_throws_away_almost_everything_it_reads_is_reported():
    scan = node(actual_rows=1333, ms=100.0, relation_name="visits", schema_name="s", rows_removed_by_filter=396000)

    (finding,) = discarded_rows(scan, context(scan))
    assert finding.issue is PlanIssue.DISCARDED_ROWS
    assert finding.evidence["rows_removed_per_loop"] == 396000
    assert "tune-queries" in finding.detail


def test_a_scan_that_keeps_most_of_what_it_reads_is_left_alone():
    """An index cannot help a filter that lets nearly everything through, and
    recommending one would be advice that makes things worse."""
    scan = node(actual_rows=100000, ms=100.0, rows_removed_by_filter=2000)

    assert discarded_rows(scan, context(scan)) == []


def test_a_small_number_of_discarded_rows_is_not_worth_an_index():
    scan = node(actual_rows=1, ms=100.0, rows_removed_by_filter=200)

    assert discarded_rows(scan, context(scan)) == []


def test_repeated_loops_are_multiplied_out_in_the_detail():
    scan = node(actual_rows=1, ms=1.0, loops=500, rows_removed_by_filter=5000)

    (finding,) = discarded_rows(scan, context(scan))
    assert "2,500,000 rows discarded in all" in finding.detail


# -- Spills -----------------------------------------------------------------


def test_a_sort_that_went_to_disk_is_reported_with_what_it_wrote():
    sort = node("Sort", ms=200.0, sort_method="external merge", sort_space_used=52896, sort_space_type="Disk")

    (finding,) = sort_spilled(sort, context(sort, work_mem="4MB"))
    assert finding.evidence["disk_kb"] == 52896
    assert finding.remediation == ["SET work_mem = '78MB';"]


def test_a_sort_that_stayed_in_memory_is_not_reported():
    sort = node("Sort", ms=200.0, sort_method="quicksort", sort_space_used=1200, sort_space_type="Memory")

    assert sort_spilled(sort, context(sort)) == []


def test_a_hash_split_into_batches_is_reported():
    hash_node = node("Hash", ms=300.0, hash_batches=512, original_hash_batches=512, peak_memory_usage=88)

    (finding,) = hash_spilled(hash_node, context(hash_node))
    assert finding.evidence["batches"] == 512
    assert finding.evidence["rebatched_at_runtime"] is False


def test_a_hash_that_grew_its_batches_at_run_time_names_the_estimate_as_well():
    """Batching that was planned is a memory problem. Batching that appeared
    while the query ran means the row estimate feeding it was wrong too, and
    that is a different fix."""
    hash_node = node("Hash", ms=300.0, hash_batches=512, original_hash_batches=16, peak_memory_usage=88)

    (finding,) = hash_spilled(hash_node, context(hash_node))
    assert finding.evidence["rebatched_at_runtime"] is True
    assert "row estimate" in finding.detail


def test_a_single_batch_hash_is_not_a_spill():
    assert hash_spilled(node("Hash", hash_batches=1), context(node("Hash"))) == []


def test_a_lossy_bitmap_is_reported_with_the_recheck_it_caused():
    bitmap = node("Bitmap Heap Scan", ms=90.0, lossy_heap_blocks=4000, exact_heap_blocks=10, rows_removed_by_index_recheck=250000)

    (finding,) = lossy_bitmap(bitmap, context(bitmap))
    assert finding.evidence["lossy_heap_blocks"] == 4000
    assert "250,000" in finding.detail


def test_an_exact_bitmap_is_not_reported():
    bitmap = node("Bitmap Heap Scan", ms=90.0, lossy_heap_blocks=0, exact_heap_blocks=4000)

    assert lossy_bitmap(bitmap, context(bitmap)) == []


# -- workers_not_launched ---------------------------------------------------


def test_workers_asked_for_and_not_granted_are_reported():
    gather = node("Gather", ms=500.0, workers_planned=4, workers_launched=1)

    (finding,) = workers_not_launched(gather, context(gather))
    assert finding.evidence == {"workers_planned": 4, "workers_launched": 1}


def test_workers_all_granted_are_not_reported():
    assert workers_not_launched(node("Gather", workers_planned=2, workers_launched=2), context(node("Gather"))) == []


# -- Everything at once -----------------------------------------------------


def test_a_healthy_plan_produces_no_findings_from_any_rule():
    """The direction that makes an empty result mean something."""
    scan = node("Index Scan", plan_rows=1000, actual_rows=1010, ms=5.0, relation_name="visits", schema_name="s")
    sort = node("Sort", plan_rows=1000, actual_rows=1010, ms=8.0, sort_method="quicksort", sort_space_type="Memory", children=[scan])
    root = node("Limit", plan_rows=100, actual_rows=100, ms=8.5, children=[sort])
    ctx = context(root, columns={"s.visits": {"country", "city"}})

    assert [finding for rule in RULES for finding in rule(root, ctx)] == []
