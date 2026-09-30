"""What a plan is asked about, one question per rule.

Each rule is a pure function of the parsed plan and a :class:`RuleContext` of
catalog facts gathered once, so a rule can be fired at a hand-built tree in a
unit test without a database anywhere near it.

The rules divide into two kinds, and the difference matters when reading the
output. :data:`~pg_debug.plans.result.PlanIssue.SORT_SPILLED` and its neighbours
are read straight off the plan: the server said it sorted on disk, so it sorted
on disk. :data:`~pg_debug.plans.result.PlanIssue.CORRELATED_COLUMNS` is an
inference — the estimate was wrong, several columns of one table were
restricted at once, and nothing in ``pg_statistic_ext`` covers them, which is
the signature of correlation but is also consistent with other causes. It is
reported with the statement that settles it, which either moves the estimate or
does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence
from typing import Set
from typing import Tuple

from ..severity import Severity
from .metrics import conditions
from .metrics import inclusive_ms
from .metrics import is_misestimated
from .metrics import loops
from .metrics import lowest_misestimates
from .metrics import qualified_relation
from .metrics import self_ms
from .metrics import walk
from .result import MIN_REPORTABLE_SHARE
from .result import PlanFinding
from .result import PlanIssue
from .result import severity_for_share
from .tree import PlanNode

#: A scan has to discard at least this many rows, and this many times more than
#: it returned, before the filter is worth an index.
MIN_DISCARDED_ROWS = 1000
DISCARD_RATIO = 10.0

#: A nested loop has to have run at least this many iterations before the
#: count is worth reading anything into.
MIN_LOOP_ITERATIONS = 100

#: Longest identifier PostgreSQL will accept, which bounds a generated
#: statistics name.
MAX_IDENTIFIER_LENGTH = 63

#: Words that appear in a rendered plan condition and are never a bare column.
_NOT_COLUMNS = frozenset(
    {"and", "or", "not", "null", "is", "in", "any", "all", "case", "when", "then", "else", "end", "true", "false", "like", "between"}
)

_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_CAST_RE = re.compile(r"::\s*(?:\"[^\"]+\"|[a-zA-Z_][\w$]*)(?:\s*\[\s*\])*")
_IDENTIFIER_RE = re.compile(r'"([^"]+)"|([a-zA-Z_][a-zA-Z0-9_$]*)')


@dataclass(frozen=True)
class RuleContext:
    """The catalog facts a rule needs, read once for the whole plan.

    Attributes:
        parents: Parent of each node, by :func:`id`. A misestimate is only
            interesting together with how far up the tree it spread, and the
            plan JSON has no upward links.
        total_ms: The tree's total self time, which every share is taken
            against.
        min_ratio: How far out an estimate has to be to count.
        stale: Relations whose statistics no longer describe them, keyed by
            qualified name.
        covered: For each relation, the column sets already covered by an
            extended statistics object.
        columns: Each relation's real column names, which is how a column is
            told apart from a type name in a rendered condition.
        work_mem: The setting as this session sees it, quoted back in the
            remediation so nobody has to go and look it up.
    """

    parents: Dict[int, Optional[PlanNode]] = field(default_factory=dict)
    total_ms: float = 0.0
    min_ratio: float = 10.0
    stale: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    covered: Dict[str, List[Set[str]]] = field(default_factory=dict)
    columns: Dict[str, Set[str]] = field(default_factory=dict)
    work_mem: Optional[str] = None

    def share(self, ms: float) -> float:
        """``ms`` as a share of the query's work."""
        if self.total_ms <= 0:
            return 0.0
        return ms / self.total_ms


def _fmt(value: Any) -> str:
    """Thousands separators, because these numbers are the argument."""
    return f"{value:,}" if isinstance(value, int) else str(value)


def _contaminated_root(node: PlanNode, context: RuleContext) -> PlanNode:
    """The highest ancestor still carrying this node's estimation error.

    Walking up while each parent is *also* misestimated measures how far the
    mistake spread. That region is what the error actually cost — the scan
    itself is often trivial, and the damage is the join above it that chose its
    strategy from the wrong number.
    """
    top = node
    while True:
        parent = context.parents.get(id(top))
        if parent is None or not is_misestimated(parent, context.min_ratio):
            return top
        top = parent


def _columns_in(text: str, known: Set[str]) -> List[str]:
    """The table's own columns named in a rendered plan condition.

    PostgreSQL renders a condition as its own expression output, not as SQL a
    parser will take back — ``((visits.country = 'c7'::text) AND …)``. Rather
    than parse that, this pulls out every identifier and keeps the ones the
    catalog says are columns of this table. A name that is not a column of the
    table cannot survive the intersection, so a type name or an alias cannot be
    mistaken for one.
    """
    stripped = _CAST_RE.sub(" ", _LITERAL_RE.sub(" ", text))
    found: List[str] = []
    for quoted, bare in _IDENTIFIER_RE.findall(stripped):
        name = quoted or bare
        if not quoted and name.lower() in _NOT_COLUMNS:
            continue
        if name in known and name not in found:
            found.append(name)
    return found


def _statistics_name(relation: str, columns: Sequence[str]) -> str:
    """A name for a generated statistics object, in the table's own schema.

    Qualified deliberately. ``CREATE STATISTICS`` puts an unqualified object in
    the first schema on the search_path rather than beside the table it
    describes, so two same-named tables in two schemas would collide over one
    name — and the collision surfaces as a duplicate-object error against a
    statistics object the user has never heard of.
    """
    schema, _, table = relation.rpartition(".")
    name = "_".join([table, *columns, "stx"])[:MAX_IDENTIFIER_LENGTH]
    return f"{schema}.{name}" if schema else name


def row_estimate(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """Report the lowest node whose estimate diverged, and what it misled."""
    findings: List[PlanFinding] = []
    for node in lowest_misestimates(root, context.min_ratio):
        estimated, actual = node.plan_rows, node.actual_rows or 0
        top = _contaminated_root(node, context)
        cost = inclusive_ms(top)
        share = context.share(cost)
        direction = "an underestimate" if actual > estimated else "an overestimate"
        relation = qualified_relation(node)
        where = f" on {relation}" if relation else ""

        detail = (
            "Every estimate above this node inherited the error, and the plan was chosen from it."
            if top is node
            else f"The error spread up to the {top.node_type} above it, which chose its strategy from the wrong number."
        )
        if node.actual_loops and node.actual_loops > 1:
            detail += f" Counts are per loop, and this node ran {_fmt(node.actual_loops)} times."

        findings.append(
            PlanFinding(
                issue=PlanIssue.ROW_ESTIMATE,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=(
                    f"The {node.node_type}{where} expected {_fmt(estimated)} rows and found {_fmt(actual)} "
                    f"— {direction} of {round(max(estimated, actual) / max(min(estimated, actual), 1))}x."
                ),
                detail=detail,
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "estimated_rows": estimated,
                    "actual_rows": actual,
                    "loops": loops(node),
                    "contaminated_node": top.node_type,
                },
            )
        )
    return findings


def stale_statistics(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """A misestimate whose table has not been analyzed recently enough.

    This is the first thing to rule out, and the cheapest: if the planner is
    working from numbers taken when the table was a tenth of its present size,
    nothing else about the estimate is worth reasoning about yet.
    """
    findings: List[PlanFinding] = []
    for node in lowest_misestimates(root, context.min_ratio):
        relation = qualified_relation(node)
        stale = context.stale.get(relation or "")
        if not stale:
            continue

        cost = inclusive_ms(_contaminated_root(node, context))
        share = context.share(cost)
        if stale.get("never_analyzed"):
            reason = "It has never been analyzed, so the planner is working from a default guess."
        else:
            changed = stale.get("changed_rows") or 0
            fraction = stale.get("changed_fraction") or 0.0
            reason = (
                f"{_fmt(int(changed))} rows have changed since the last ANALYZE at "
                f"{stale.get('last_analyzed')}, which is {fraction:.0%} of the table."
            )

        findings.append(
            PlanFinding(
                issue=PlanIssue.STALE_STATISTICS,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=f"{relation} has statistics that no longer describe it, which is enough on its own to explain the estimate.",
                detail=reason,
                cost_ms=cost,
                share_of_time=share,
                evidence={k: stale.get(k) for k in ("last_analyzed", "changed_rows", "changed_fraction", "never_analyzed")},
                remediation=[f"ANALYZE {relation};"],
            )
        )
    return findings


def correlated_columns(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """An underestimate over several columns of one table, with no extended statistics.

    Without an extended statistics object the planner multiplies each column's
    selectivity as though the columns were independent. When they are not — a
    city and the country it is in — the product is far smaller than the truth,
    which is why this only fires on an *under*estimate. An overestimate has some
    other cause and ``CREATE STATISTICS`` would not touch it.
    """
    findings: List[PlanFinding] = []
    for node in lowest_misestimates(root, context.min_ratio):
        relation = qualified_relation(node)
        if not relation or (node.actual_rows or 0) <= node.plan_rows:
            continue

        known = context.columns.get(relation)
        if not known:
            continue

        text = " AND ".join(conditions(node))
        if not text or re.search(r"\bOR\b", text):
            # An OR is not a product of selectivities, so this is not the
            # independence assumption going wrong and extended statistics are
            # not the answer.
            continue

        columns = _columns_in(text, known)
        if len(columns) < 2:
            continue
        if any(set(columns) <= existing for existing in context.covered.get(relation, [])):
            # Already covered. If the estimate is still wrong, the cause is
            # something else and saying "add statistics" would be noise.
            continue

        cost = inclusive_ms(_contaminated_root(node, context))
        share = context.share(cost)
        listed = ", ".join(columns)
        name = _statistics_name(relation, columns)

        findings.append(
            PlanFinding(
                issue=PlanIssue.CORRELATED_COLUMNS,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=f"{relation} is restricted on {listed} at once, and nothing tells the planner those columns are related.",
                detail=(
                    "Lacking extended statistics the planner multiplies the columns' selectivities as if they were "
                    "independent, which is exactly how an estimate comes out far below the truth. Run the statements "
                    "below and re-run this command: either the estimate moves, or the columns were not the cause."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "columns": columns,
                    "condition": text,
                    "estimated_rows": node.plan_rows,
                    "actual_rows": node.actual_rows,
                    "existing_statistics": [sorted(c) for c in context.covered.get(relation, [])],
                },
                remediation=[
                    f"CREATE STATISTICS {name} (dependencies, mcv) ON {listed} FROM {relation};",
                    f"ANALYZE {relation};",
                ],
            )
        )
    return findings


def nested_loop_blowup(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """A nested loop whose inner side ran far more often than planned for.

    A nested loop is the right choice over a handful of outer rows and the
    wrong one over a hundred thousand, and the planner commits to it from the
    outer estimate. This is what that mistake looks like from the other side.
    """
    findings: List[PlanFinding] = []
    for node in walk(root):
        if node.node_type != "Nested Loop" or len(node.children) < 2:
            continue
        outer, inner = _join_sides(node)
        if outer is None or inner is None or inner.actual_loops is None:
            continue

        expected = max(outer.plan_rows, 1)
        ran = inner.actual_loops
        if ran < MIN_LOOP_ITERATIONS or ran < context.min_ratio * expected:
            continue

        cost = inclusive_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue

        findings.append(
            PlanFinding(
                issue=PlanIssue.NESTED_LOOP_BLOWUP,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=qualified_relation(inner),
                summary=(
                    f"A Nested Loop planned for {_fmt(expected)} iterations ran {_fmt(ran)} of them, re-executing its {inner.node_type} each time."
                ),
                detail=(
                    "The loop was chosen because the outer side looked small. Correcting the estimate underneath it is "
                    "the fix; the join method follows from that rather than needing to be forced."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "planned_iterations": expected,
                    "actual_iterations": ran,
                    "outer_node": outer.node_type,
                    "inner_node": inner.node_type,
                },
            )
        )
    return findings


def discarded_rows(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """A scan reading rows in order to throw them away.

    This is the shape an index is for: the rows were fetched, tested and
    dropped, and every one of them was a page read that need not have happened.
    """
    findings: List[PlanFinding] = []
    for node in walk(root):
        removed = node.rows_removed_by_filter
        if not removed:
            continue
        kept = max(node.actual_rows or 0, 0)
        if removed < MIN_DISCARDED_ROWS or removed < DISCARD_RATIO * max(kept, 1):
            continue

        cost = self_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue

        run = loops(node)
        relation = qualified_relation(node)
        findings.append(
            PlanFinding(
                issue=PlanIssue.DISCARDED_ROWS,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=(
                    f"The {node.node_type}{f' on {relation}' if relation else ''} read {_fmt(removed + kept)} rows "
                    f"per loop and kept {_fmt(kept)}, discarding {removed / max(removed + kept, 1):.0%} of them."
                ),
                detail=(
                    "An index over the filtered columns would let the scan skip those rows rather than fetch and reject "
                    "them. `pg-debug tune-queries` will cost one out and say whether it is worth its maintenance."
                    + (f" The scan ran {_fmt(run)} times, so that is {_fmt(removed * run)} rows discarded in all." if run > 1 else "")
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "rows_removed_per_loop": removed,
                    "rows_returned_per_loop": kept,
                    "loops": run,
                    "filter": node.filter,
                },
            )
        )
    return findings


def sort_spilled(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """A sort that did not fit in work_mem and went to disk."""
    findings: List[PlanFinding] = []
    for node in walk(root):
        if node.sort_space_type != "Disk":
            continue
        used_kb = node.sort_space_used or 0
        cost = self_ms(node)
        share = context.share(cost)
        findings.append(
            PlanFinding(
                issue=PlanIssue.SORT_SPILLED,
                severity=severity_for_share(share),
                node_type=node.node_type,
                summary=f"The sort spilled to disk, writing {_fmt(used_kb)} kB ({node.sort_method}).",
                detail=_work_mem_detail(used_kb, context),
                cost_ms=cost,
                share_of_time=share,
                evidence={"sort_method": node.sort_method, "disk_kb": used_kb, "work_mem": context.work_mem},
                remediation=[f"SET work_mem = '{_suggested_work_mem(used_kb)}';"],
            )
        )
    return findings


def hash_spilled(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """A hash that did not fit in work_mem and was split into batches."""
    findings: List[PlanFinding] = []
    for node in walk(root):
        batches = node.hash_batches
        if not batches or batches <= 1:
            continue
        cost = inclusive_ms(node)
        share = context.share(cost)
        original = node.original_hash_batches
        rebatched = bool(original and batches > original)
        detail = (
            "Each batch is written out and read back, so the join pays for the whole hash side twice over. "
            f"The hash peaked at {_fmt(node.peak_memory_usage or 0)} kB"
            f"{f', against a work_mem of {context.work_mem}' if context.work_mem else ''}."
        )
        if rebatched:
            detail += (
                f" It started with {_fmt(original or 0)} batches and grew to {_fmt(batches)}, which means the row estimate "
                "feeding it was too low as well — the batching was not planned, it was forced at run time."
            )
        findings.append(
            PlanFinding(
                issue=PlanIssue.HASH_SPILLED,
                severity=severity_for_share(share),
                node_type=node.node_type,
                summary=f"The hash was split into {_fmt(batches)} batches because it did not fit in work_mem.",
                detail=detail,
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "batches": batches,
                    "original_batches": original,
                    "rebatched_at_runtime": rebatched,
                    "peak_memory_kb": node.peak_memory_usage,
                    "work_mem": context.work_mem,
                },
                remediation=[f"SET work_mem = '{_suggested_work_mem((node.peak_memory_usage or 0) * batches)}';"],
            )
        )
    return findings


def lossy_bitmap(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """A bitmap too large for work_mem, degraded to whole-page granularity.

    Once the bitmap goes lossy it stops recording which rows on a page matched,
    so the heap scan reads every row on those pages and re-tests them. The
    ``Rows Removed by Index Recheck`` on the same node is that cost.
    """
    findings: List[PlanFinding] = []
    for node in walk(root):
        lossy = node.lossy_heap_blocks
        if not lossy:
            continue
        cost = self_ms(node)
        share = context.share(cost)
        rechecked = node.rows_removed_by_index_recheck or 0
        findings.append(
            PlanFinding(
                issue=PlanIssue.LOSSY_BITMAP,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=qualified_relation(node),
                summary=f"The bitmap outgrew work_mem and went lossy over {_fmt(lossy)} pages.",
                detail=(
                    "A lossy bitmap records that a page has a match, not which row, so every row on those pages is read "
                    f"back and re-tested. That recheck discarded {_fmt(rechecked)} rows here."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "lossy_heap_blocks": lossy,
                    "exact_heap_blocks": node.exact_heap_blocks,
                    "rows_removed_by_recheck": rechecked,
                    "work_mem": context.work_mem,
                },
                remediation=["-- Raise work_mem for this query so the bitmap stays exact, or narrow the index condition."],
            )
        )
    return findings


def workers_not_launched(root: PlanNode, context: RuleContext) -> List[PlanFinding]:
    """Parallel workers the plan asked for and the server did not grant.

    Not a fault in the query. The plan's costs were worked out assuming the
    workers, so the query ran a plan chosen for parallelism without it.
    """
    findings: List[PlanFinding] = []
    for node in walk(root):
        planned, launched = node.workers_planned, node.workers_launched
        if planned is None or launched is None or launched >= planned:
            continue
        cost = inclusive_ms(node)
        share = context.share(cost)
        findings.append(
            PlanFinding(
                issue=PlanIssue.WORKERS_NOT_LAUNCHED,
                severity=Severity.LOW if launched else severity_for_share(share),
                node_type=node.node_type,
                summary=f"{_fmt(planned)} parallel workers were planned for and {_fmt(launched)} started.",
                detail=(
                    "The plan's costs assume the workers it asked for, so this ran a parallel plan mostly serially. "
                    "The pool is server-wide: max_parallel_workers, or another query holding them at the time."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={"workers_planned": planned, "workers_launched": launched},
            )
        )
    return findings


#: Every rule, in the order they are run. Findings are sorted afterwards, so
#: this order only decides ties.
RULES = (
    row_estimate,
    stale_statistics,
    correlated_columns,
    nested_loop_blowup,
    discarded_rows,
    sort_spilled,
    hash_spilled,
    lossy_bitmap,
    workers_not_launched,
)


def _join_sides(node: PlanNode) -> Tuple[Optional[PlanNode], Optional[PlanNode]]:
    """``(outer, inner)`` for a join node.

    ``Parent Relationship`` names them explicitly; where it is missing the plan
    lists the outer side first, which is the order EXPLAIN prints them in.
    """
    outer = next((c for c in node.children if c.parent_relationship == "Outer"), None)
    inner = next((c for c in node.children if c.parent_relationship == "Inner"), None)
    if outer is None or inner is None:
        outer, inner = node.children[0], node.children[1]
    return outer, inner


def _suggested_work_mem(kilobytes: int) -> str:
    """A work_mem comfortably above what the node needed, in round megabytes."""
    megabytes = max(4, int(kilobytes / 1024 * 1.5) + 1)
    return f"{megabytes}MB"


def _work_mem_detail(used_kb: int, context: RuleContext) -> str:
    current = f" work_mem is {context.work_mem} here." if context.work_mem else ""
    return (
        "Sorting on disk writes the data out and reads it back in runs. Raising work_mem for this query keeps it in "
        f"memory, at the cost of that much memory per sort node per connection.{current}"
    )


__all__ = [
    "DISCARD_RATIO",
    "MIN_DISCARDED_ROWS",
    "MIN_LOOP_ITERATIONS",
    "RULES",
    "RuleContext",
    "correlated_columns",
    "discarded_rows",
    "hash_spilled",
    "lossy_bitmap",
    "nested_loop_blowup",
    "row_estimate",
    "sort_spilled",
    "stale_statistics",
    "workers_not_launched",
]
