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

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from psycopg.sql import Identifier

from ..db.identifiers import table_identifier
from ..severity import Confidence
from ..severity import Severity
from .catalog import ForeignKeyFacts
from .catalog import IndexFacts
from .catalog import NameFacts
from .catalog import Policy
from .catalog import PolicyCall
from .catalog import RelationSecurity
from .catalog import calls_in
from .catalog import conjuncts
from .catalog import mask_literals
from .catalog import normalized
from .catalog import operators_in
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
        catalog: Whether the catalog was consulted at all. A plan diagnosed
            with no connection behind it has empty ``stale``, ``covered`` and
            ``columns`` not because the tables are fine but because nothing
            looked, and the findings have to say which.
        planning_ms: ``Planning Time``, when the plan carries it.
            ``auto_explain`` does not log it.
        execution_ms: ``Execution Time``, which trigger and JIT time are a
            share of: no node's time includes the triggers.
        jit: The plan's ``JIT`` section, when JIT ran.
        triggers: The plan's ``Triggers`` section.
        query_parameters: The parameter values ``auto_explain`` logged.
        security: Row-level security of each relation that has it enabled,
            with its policies, keyed by qualified name.
        functions: What the functions named in the plan's conditions and the
            policies' expressions have in common, by lower-case name.
        operators: The same for the operators in the plan's conditions, by
            symbol.
        indexes: Each relation's valid indexes.
        foreign_keys: The foreign keys behind referential-integrity
            triggers, by constraint name.
        settings: Settings as the query ran with them, by name, read in the
            query context: the ``jit_*`` thresholds a JIT finding quotes.
    """

    parents: dict[int, PlanNode | None] = field(default_factory=dict)
    total_ms: float = 0.0
    min_ratio: float = 10.0
    stale: dict[str, dict[str, Any]] = field(default_factory=dict)
    covered: dict[str, list[set[str]]] = field(default_factory=dict)
    columns: dict[str, set[str]] = field(default_factory=dict)
    work_mem: str | None = None
    catalog: bool = True
    planning_ms: float | None = None
    execution_ms: float | None = None
    jit: dict[str, Any] | None = None
    triggers: list[dict[str, Any]] = field(default_factory=list)
    query_parameters: str | None = None
    security: dict[str, RelationSecurity] = field(default_factory=dict)
    functions: dict[str, NameFacts] = field(default_factory=dict)
    operators: dict[str, NameFacts] = field(default_factory=dict)
    indexes: dict[str, list[IndexFacts]] = field(default_factory=dict)
    foreign_keys: dict[str, list[ForeignKeyFacts]] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)

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


def _columns_in(text: str, known: set[str]) -> list[str]:
    """The table's own columns named in a rendered plan condition.

    PostgreSQL renders a condition as its own expression output, not as SQL a
    parser will take back — ``((visits.country = 'c7'::text) AND …)``. Rather
    than parse that, this pulls out every identifier and keeps the ones the
    catalog says are columns of this table. A name that is not a column of the
    table cannot survive the intersection, so a type name or an alias cannot be
    mistaken for one.
    """
    stripped = _CAST_RE.sub(" ", _LITERAL_RE.sub(" ", text))
    found: list[str] = []
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


def row_estimate(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """Report the lowest node whose estimate diverged, and what it misled."""
    findings: list[PlanFinding] = []
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

        evidence: dict[str, Any] = {
            "estimated_rows": estimated,
            "actual_rows": actual,
            "loops": loops(node),
            "contaminated_node": top.node_type,
        }
        if not context.catalog and relation:
            # The two rules that would explain the estimate need the catalog,
            # and nothing consulted it. Say so here, where the reader is,
            # rather than leaving the silence to be read as a clean bill.
            detail += (
                f" Whether {relation}'s statistics are stale, or its columns are correlated with nothing telling the "
                "planner, needs this database's catalog: diagnose the plan again with a connection to find out."
            )
            evidence["catalog_checked"] = False

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
                evidence=evidence,
            )
        )
    return findings


def stale_statistics(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A misestimate whose table has not been analyzed recently enough.

    This is the first thing to rule out, and the cheapest: if the planner is
    working from numbers taken when the table was a tenth of its present size,
    nothing else about the estimate is worth reasoning about yet.
    """
    findings: list[PlanFinding] = []
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


def correlated_columns(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """An underestimate over several columns of one table, with no extended statistics.

    Without an extended statistics object the planner multiplies each column's
    selectivity as though the columns were independent. When they are not — a
    city and the country it is in — the product is far smaller than the truth,
    which is why this only fires on an *under*estimate. An overestimate has some
    other cause and ``CREATE STATISTICS`` would not touch it.
    """
    findings: list[PlanFinding] = []
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


def nested_loop_blowup(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A nested loop whose inner side ran far more often than planned for.

    A nested loop is the right choice over a handful of outer rows and the
    wrong one over a hundred thousand, and the planner commits to it from the
    outer estimate. This is what that mistake looks like from the other side.
    """
    findings: list[PlanFinding] = []
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


def discarded_rows(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A scan reading rows in order to throw them away.

    This is the shape an index is for: the rows were fetched, tested and
    dropped, and every one of them was a page read that need not have happened.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        removed = node.rows_removed_by_filter
        if not removed:
            continue
        kept = max(node.actual_rows or 0, 0)
        if removed < MIN_DISCARDED_ROWS or removed < DISCARD_RATIO * max(kept, 1):
            continue
        if _claimed_discard(node, context):
            # A rule with the specific cause reports these rows, and an index
            # over the filtered columns is not its first answer.
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


def sort_spilled(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A sort that did not fit in work_mem and went to disk."""
    findings: list[PlanFinding] = []
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


def hash_spilled(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A hash that did not fit in work_mem and was split into batches."""
    findings: list[PlanFinding] = []
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


def lossy_bitmap(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A bitmap too large for work_mem, degraded to whole-page granularity.

    Once the bitmap goes lossy it stops recording which rows on a page matched,
    so the heap scan reads every row on those pages and re-tests them. The
    ``Rows Removed by Index Recheck`` on the same node is that cost.
    """
    findings: list[PlanFinding] = []
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


def workers_not_launched(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """Parallel workers the plan asked for and the server did not grant.

    Not a fault in the query. The plan's costs were worked out assuming the
    workers, so the query ran a plan chosen for parallelism without it.
    """
    findings: list[PlanFinding] = []
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


#: An Index Only Scan has to have gone to the heap at least this often, and
#: for at least this share of its rows, before it is worth a VACUUM.
MIN_HEAP_FETCHES = 1000
HEAP_FETCH_SHARE = 0.1

#: A sequential scan has to read at least this many pages before its density
#: is worth reading anything into.
MIN_SCANNED_PAGES = 1000

#: Fewest live rows a heap page of whole rows can hold. A page is 8 kB, and a
#: row larger than about 2 kB is moved out of line to TOAST, so four rows fit
#: in any page that is not mostly dead rows or free space.
SPARSE_ROWS_PER_PAGE = 4.0

#: A LIMIT has to have skipped at least this many rows to be called an OFFSET
#: worth replacing.
MIN_SKIPPED_ROWS = 1000

#: Time no node includes has to be at least this share of the execution, and
#: this many milliseconds, to be reported.
MIN_UNATTRIBUTED_SHARE = 0.1
MIN_UNATTRIBUTED_MS = 5.0

#: A plan has to read at least this many relations for its planning time to
#: be the query's: with one there are no join orders or partitions to weigh.
MIN_PLANNED_RELATIONS = 2

#: PostgreSQL's default ``jit_above_cost``.
DEFAULT_JIT_ABOVE_COST = 100_000

_PARAM_RE = re.compile(r"\$(\d+)\b")


def _ms(value: float) -> str:
    return f"{value:,.1f} ms"


def _quoted_relation(relation: str) -> str:
    return table_identifier(relation).as_string(None)


def _quoted(name: str) -> str:
    return Identifier(name).as_string(None)


def _discards(node: PlanNode) -> tuple[int, int]:
    """``(removed, kept)`` per loop when a filter discarded enough to be worth a rule, else ``(0, kept)``."""
    removed = node.rows_removed_by_filter or 0
    kept = max(node.actual_rows or 0, 0)
    if removed < MIN_DISCARDED_ROWS or removed < DISCARD_RATIO * max(kept, 1):
        return 0, kept
    return removed, kept


def _claimed_discard(node: PlanNode, context: RuleContext) -> bool:
    """Whether a rule with the specific cause already reports this node's discarded rows.

    A CTE Scan is always claimed: it reads a CTE's output, which no index can
    serve, and :func:`cte_materialized` says what can.
    """
    if node.node_type == "CTE Scan":
        return True
    return bool(_rls_calls(node, context)) or _limit_scan(node, context) or _function_on_index(node, context) is not None


# -- Row-level security -----------------------------------------------------


def _rls_calls(node: PlanNode, context: RuleContext) -> list[tuple[Policy, PolicyCall, bool]]:
    """The policy calls this node makes once per row: ``(policy, call, inlined)``.

    A filter is evaluated for every row it is applied to, and a call in it
    with it. A call the plan names is matched by name; a call to an SQL
    function the planner inlined shows as the functions its body calls
    instead, and is matched by those, which is weaker and marked so.
    """
    relation = qualified_relation(node)
    security = context.security.get(relation or "")
    if security is None:
        return []
    text = " AND ".join(t for t in (node.filter, node.join_filter) if t)
    if not text:
        return []
    calls = calls_in(text)
    found: list[tuple[Policy, PolicyCall, bool]] = []
    for policy in security.policies:
        for call in policy.calls:
            function = call.function
            direct = any(c.name == function.name and c.schema in (None, function.schema) for c in calls)
            inlined = not direct and function.inlinable and any(c.name.lower() in function.body_calls for c in calls)
            if direct or inlined:
                found.append((policy, call, inlined))
    return found


def rls_per_row_call(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A row-level security policy calling a STABLE function once per row.

    ``USING (user_id = auth.uid())`` reads as one call, and runs as one per
    row: the policy's expression is added to every scan of the table as a
    filter, and a STABLE function's result is not cached between rows.
    ``USING (user_id = (SELECT auth.uid()))`` makes it an InitPlan, run once
    for the statement. The rows the filter discards are this finding's, not
    an index's: the call is the first thing to fix.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        matches = _rls_calls(node, context)
        if not matches:
            continue
        cost = self_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue

        relation = qualified_relation(node) or ""
        run = loops(node)
        removed = node.rows_removed_by_filter or node.rows_removed_by_join_filter or 0
        kept = max(node.actual_rows or 0, 0)
        tested = (removed + kept) * run

        by_policy: dict[str, list[tuple[Policy, PolicyCall, bool]]] = {}
        for match in matches:
            by_policy.setdefault(match[0].name, []).append(match)

        for group in by_policy.values():
            policy = group[0][0]
            texts = list(dict.fromkeys(call.text for _, call, _ in group))
            wrapped = ", ".join(f"(SELECT {text})" for text in texts)
            inlined = all(flag for _, _, flag in group)
            listed = ", ".join(texts)

            detail = (
                f"{listed} {'is' if len(texts) == 1 else 'are'} STABLE: the result cannot change during the statement, but "
                f"written into the policy it is evaluated for every row the policy is applied to. Written as {wrapped}, "
                "PostgreSQL evaluates it once, as an InitPlan, and compares each row with the result."
            )
            if inlined:
                body = sorted({name for _, call, _ in group for name in call.function.body_calls})
                detail += (
                    f" The plan does not name {listed}: it is an SQL function the planner inlined, and the filter calls "
                    f"{', '.join(body)} from its body instead, which is the same work per row. That is matched by name, so "
                    "it is a candidate: re-run after the change and the per-row call is gone, or this was not it."
                )
            if removed >= MIN_DISCARDED_ROWS:
                detail += (
                    f" The filter discarded {_fmt(removed)} rows{' per loop' if run > 1 else ''}; pg-debug does not suggest "
                    "an index for them until the call is made once. If the scan still reads rows only to discard them, an "
                    "index on the column the policy compares with lets it skip them, and `pg-debug tune-queries` will cost one."
                )

            remediation: list[str] = []
            if policy.rewritten_qual or policy.rewritten_check:
                statement = f"ALTER POLICY {_quoted(policy.name)} ON {_quoted_relation(relation)}"
                if policy.rewritten_qual:
                    statement += f" USING ({policy.rewritten_qual})"
                if policy.rewritten_check:
                    statement += f" WITH CHECK ({policy.rewritten_check})"
                remediation.append(statement + ";")

            findings.append(
                PlanFinding(
                    issue=PlanIssue.RLS_PER_ROW_CALL,
                    severity=severity_for_share(share),
                    node_type=node.node_type,
                    relation=relation,
                    summary=(
                        f"Row-level security policy {policy.name} on {relation} calls {listed} once per row: up to "
                        f"{_fmt(tested)} calls in this {node.node_type}."
                    ),
                    detail=detail,
                    cost_ms=cost,
                    share_of_time=share,
                    evidence={
                        "policy": policy.name,
                        "command": policy.command,
                        "roles": list(policy.roles),
                        "policy_expression": policy.qual,
                        "functions": sorted({call.function.qualified for _, call, _ in group}),
                        "volatility": "stable",
                        "inlined": inlined,
                        "filter": node.filter or node.join_filter,
                        "rows_tested": tested,
                        "rows_removed_per_loop": removed,
                        "loops": run,
                    },
                    remediation=remediation,
                    confidence=Confidence.CANDIDATE if inlined else Confidence.PROVEN,
                )
            )
    return findings


def rls_leaky_predicate(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A condition the policy keeps from its index, because it is not LEAKPROOF.

    Under row-level security a condition from the query may be evaluated
    before the policy's own only if every function in it is LEAKPROOF —
    promises to reveal nothing about its arguments, not even in an error
    message. One that is not is applied after the policy, as a filter, and
    cannot be an index condition, whatever index exists for it. The plan
    shows the policy applied in this node; the catalog shows the index and
    that the operator or function is not LEAKPROOF.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        relation = qualified_relation(node)
        security = context.security.get(relation or "")
        if security is None or not security.policies or not node.filter:
            continue
        removed, kept = _discards(node)
        if not removed:
            continue
        cost = self_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue

        policy_parts = {normalized(part) for policy in security.policies if policy.qual for part in conjuncts(policy.qual)}
        node_parts = [part for text in conditions(node) for part in conjuncts(text)]
        if not any(normalized(part) in policy_parts for part in node_parts):
            # Nothing shows the policy was applied here: the plan was taken
            # as a role that bypasses it, or the policy is not the one shown.
            continue

        known = context.columns.get(relation or "", set())
        indexes = [index for index in context.indexes.get(relation or "", []) if not index.partial and index.keys]
        for part in conjuncts(node.filter):
            if normalized(part) in policy_parts:
                continue
            leaky = [symbol for symbol in operators_in(part) if (facts := context.operators.get(symbol)) and facts.leakproof is False]
            leaky += [call.name for call in calls_in(part) if (facts := context.functions.get(call.name.lower())) and facts.leakproof is False]
            if not leaky:
                continue
            columns = _columns_in(part, known)
            usable = [index for index in indexes if index.keys[0] in columns]
            if not usable:
                continue

            names = ", ".join(dict.fromkeys(leaky))
            findings.append(
                PlanFinding(
                    issue=PlanIssue.RLS_LEAKY_PREDICATE,
                    severity=severity_for_share(share),
                    node_type=node.node_type,
                    relation=relation,
                    summary=(
                        f"Row-level security keeps {part} on {relation} from using index {usable[0].name}: {names} "
                        f"{'is' if len(set(leaky)) == 1 else 'are'} not LEAKPROOF."
                    ),
                    detail=(
                        "Under row-level security a condition from the query is evaluated before the policy's only when "
                        "every function in it is LEAKPROOF, a promise that it reveals nothing about the rows it is given. "
                        f"{names} makes no such promise, so the policy is applied first and this condition after it, as a "
                        f"filter, and the index on {', '.join(columns)} cannot serve it. The {node.node_type} read "
                        f"{_fmt(removed + kept)} rows to keep {_fmt(kept)}. A role that bypasses row-level security — the "
                        "table's owner — gets the index scan, which is why the plan can differ between roles. The operator "
                        "is matched by its name, not its argument types, so this is a candidate."
                    ),
                    cost_ms=cost,
                    share_of_time=share,
                    evidence={
                        "condition": part,
                        "not_leakproof": list(dict.fromkeys(leaky)),
                        "indexes": [index.name for index in usable],
                        "policies": [policy.name for policy in security.policies],
                        "rows_removed_per_loop": removed,
                        "rows_returned_per_loop": kept,
                    },
                    remediation=[
                        "-- Give the policy's own condition an index, so the policy narrows the rows before this one is tested.",
                        f"-- Or, only if {names} truly cannot reveal its arguments, a superuser can mark the function LEAKPROOF.",
                    ],
                    confidence=Confidence.CANDIDATE,
                )
            )
            break
    return findings


# -- Estimates from parameters ------------------------------------------------


def _subplan_nodes(root: PlanNode) -> set[int]:
    """The nodes under a SubPlan, whose ``$n`` are set by the plan around them."""
    inside: set[int] = set()

    def visit(node: PlanNode, under: bool) -> None:
        under = under or node.parent_relationship == "SubPlan"
        if under:
            inside.add(id(node))
        for child in node.children:
            visit(child, under)

    visit(root, False)
    return inside


def generic_plan(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A misestimate on a condition comparing with a ``$n`` parameter.

    A plan whose conditions still read ``$1`` is a generic plan: built once
    for any value of the parameter, and estimated from the column's average
    rather than from the value. ``auto_explain`` logs it that way for a
    prepared statement once PostgreSQL has switched to the generic plan,
    after five executions, or always under ``plan_cache_mode =
    force_generic_plan``. The ``$n`` an InitPlan returns, and those inside a
    SubPlan, are the plan's own and are not counted.
    """
    internal = {number for node in walk(root) if node.subplan_name for number in _PARAM_RE.findall(node.subplan_name)}
    inside = _subplan_nodes(root)
    findings: list[PlanFinding] = []
    for node in lowest_misestimates(root, context.min_ratio):
        if id(node) in inside:
            continue
        params = sorted(
            {number for text in conditions(node) for number in _PARAM_RE.findall(mask_literals(text)) if number not in internal},
            key=int,
        )
        if not params:
            continue

        cost = inclusive_ms(_contaminated_root(node, context))
        share = context.share(cost)
        listed = ", ".join(f"${number}" for number in params)
        values = f" auto_explain logged the values as {context.query_parameters}." if context.query_parameters else ""
        relation = qualified_relation(node)
        findings.append(
            PlanFinding(
                issue=PlanIssue.GENERIC_PLAN,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=(
                    f"The {node.node_type}{f' on {relation}' if relation else ''} was estimated for {listed} without "
                    "its value: this is a generic plan."
                ),
                detail=(
                    "A generic plan is planned once for every value of its parameters, so its estimates come from the "
                    "column's average, and a value that is rare or common is estimated as neither. A custom plan is "
                    f"planned for the values at hand.{values} Setting plan_cache_mode to force_custom_plan for this "
                    "statement's role or transaction (PostgreSQL 12 and later) plans each execution for its values; run "
                    "it again and either the estimate moves, or the parameter was not the cause."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "parameters": [f"${number}" for number in params],
                    "query_parameters": context.query_parameters,
                    "estimated_rows": node.plan_rows,
                    "actual_rows": node.actual_rows,
                    "conditions": conditions(node),
                },
                remediation=["SET plan_cache_mode = force_custom_plan;"],
                confidence=Confidence.CANDIDATE,
            )
        )
    return findings


# -- Scans that do more than they return ----------------------------------------


def _limit_child(node: PlanNode) -> PlanNode | None:
    """The node a Limit draws its rows from, leaving out an InitPlan for its count."""
    return next((child for child in node.children if child.parent_relationship in (None, "Outer")), None)


def _limit_scan(node: PlanNode, context: RuleContext) -> bool:
    """Whether this is an index scan straight under a Limit, discarding most of what it read."""
    parent = context.parents.get(id(node))
    return (
        parent is not None
        and parent.node_type == "Limit"
        and node.node_type in ("Index Scan", "Index Only Scan")
        and _limit_child(parent) is node
        and bool(_discards(node)[0])
    )


def limit_scan_filter(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """An index walked in order for a LIMIT, discarding most of what it read.

    With no Sort between them, the scan supplies the LIMIT's order: the
    planner chose to walk an index in that order and stop after enough
    matching rows, betting that matches are spread evenly through it. Here
    they were not, and the filter threw away rows by the thousand before the
    LIMIT was satisfied.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        if not _limit_scan(node, context):
            continue
        removed, kept = _discards(node)
        cost = self_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue

        relation = qualified_relation(node)
        known = context.columns.get(relation or "", set())
        index = next((i for i in context.indexes.get(relation or "", []) if i.name == node.index_name), None)
        filtered = _columns_in(node.filter or "", known)
        equalities = all(" = " in part and len(_columns_in(part, known)) == 1 for part in conjuncts(node.filter or ""))

        remediation: list[str] = []
        suggestion = ""
        if relation and index and filtered and equalities:
            keys = [*filtered, *[key for key in index.keys if key not in filtered]]
            remediation.append(f"CREATE INDEX CONCURRENTLY ON {_quoted_relation(relation)} ({', '.join(keys)});")
            suggestion = (
                f" An index on ({', '.join(keys)}) finds the matching rows already in {index.name}'s order. It is a "
                "candidate: cost it with `pg-debug explain --hypothetical-index` before creating it."
            )
        elif not context.catalog:
            suggestion = " Which index would serve both needs the table's columns and indexes: diagnose with a connection to have one named."

        findings.append(
            PlanFinding(
                issue=PlanIssue.LIMIT_SCAN_FILTER,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=(f"The LIMIT walked {node.index_name or 'an index'} in order and discarded {_fmt(removed)} rows to find {_fmt(kept)}."),
                detail=(
                    "With no sort between them, the index supplies the LIMIT's order, and the planner bet that rows "
                    f"matching {node.filter} are spread evenly through it, so a few would turn up early. They did not. "
                    "An index that leads with the filtered columns and then the ordering ones serves both." + suggestion
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "index": node.index_name,
                    "filter": node.filter,
                    "rows_removed": removed,
                    "rows_returned": kept,
                    "index_keys": list(index.keys) if index else None,
                },
                remediation=remediation,
                confidence=Confidence.PROVEN,
            )
        )
    return findings


def _function_on_index(node: PlanNode, context: RuleContext) -> tuple[Any, str, IndexFacts] | None:
    """A call in this node's filter on a column some index leads with, when the filter discards enough.

    Returns ``(call, column, index)``, or None.
    """
    if not node.filter or not _discards(node)[0]:
        return None
    relation = qualified_relation(node) or ""
    known = context.columns.get(relation)
    indexes = [index for index in context.indexes.get(relation, []) if not index.partial and index.keys]
    if not known or not indexes:
        return None
    for call in calls_in(node.filter):
        columns = _columns_in(call.arguments, known)
        if len(columns) != 1:
            continue
        plain = next((index for index in indexes if index.keys[0] == columns[0]), None)
        if plain is None:
            continue
        expression = normalized(_CAST_RE.sub("", _without_alias(call.text, node)))
        if any(normalized(_CAST_RE.sub("", index.keys[0])) == expression for index in indexes):
            continue
        return call, columns[0], plain
    return None


def _without_alias(text: str, node: PlanNode) -> str:
    """A rendered expression with the node's alias taken off its columns."""
    alias = node.alias or node.relation_name
    if not alias:
        return text
    return re.sub(rf'(?<![\w$"]){re.escape(alias)}\.', "", text)


def function_on_indexed_column(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A filter applying a function to a column an index exists on.

    An index on ``email`` orders the values of ``email``, and says nothing
    about ``lower(email)``: the planner cannot use it for the condition, and
    the scan reads every row to compute the function and compare. An index on
    the expression itself would serve it — when the function is IMMUTABLE,
    which an index requires.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        found = _function_on_index(node, context)
        if found is None:
            continue
        call, column, index = found
        removed, kept = _discards(node)
        cost = self_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue

        relation = qualified_relation(node) or ""
        expression = _without_alias(call.text, node)
        facts = context.functions.get(call.name.lower())
        remediation: list[str] = []
        if facts is not None and facts.immutable:
            remediation.append(f"CREATE INDEX CONCURRENTLY ON {_quoted_relation(relation)} (({expression}));")
            verdict = f"{call.name} is IMMUTABLE, so an index on the expression is possible, and the condition would use it."
        elif facts is not None and facts.immutable is False:
            verdict = f"{call.name} is not IMMUTABLE, so no index can be built on it: compare {column} itself instead, if the query allows."
        else:
            verdict = f"Whether {call.name} is IMMUTABLE, which an index on it needs, could not be told from its name."

        findings.append(
            PlanFinding(
                issue=PlanIssue.FUNCTION_ON_INDEXED_COLUMN,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=f"The filter applies {call.name}() to {column}, so index {index.name} on {column} cannot serve it.",
                detail=(
                    f"An index on {column} orders {column}'s values, not {expression}'s, so the {node.node_type} read "
                    f"{_fmt(removed + kept)} rows and computed {call.name}() for each to keep {_fmt(kept)}. {verdict} "
                    "This rests on reading the condition's text, so it is a candidate."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "expression": expression,
                    "column": column,
                    "index": index.name,
                    "filter": node.filter,
                    "rows_removed_per_loop": removed,
                    "rows_returned_per_loop": kept,
                    "immutable": facts.immutable if facts else None,
                },
                remediation=remediation,
                confidence=Confidence.CANDIDATE,
            )
        )
    return findings


def or_across_join(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A join condition ORing comparisons across the two sides.

    A hash join or a merge join needs one equality to match on. ``a.x = b.y
    OR a.z = b.y`` offers two alternatives, so the only join that can apply
    it is a nested loop testing every pair, and ``Rows Removed by Join
    Filter`` counts the pairs it tested and threw away.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        if node.node_type != "Nested Loop" or not node.join_filter or len(node.children) < 2:
            continue
        masked = mask_literals(node.join_filter)
        if not re.search(r"\bOR\b", masked):
            continue
        removed = node.rows_removed_by_join_filter or 0
        kept = max(node.actual_rows or 0, 0)
        if removed < MIN_DISCARDED_ROWS or removed < DISCARD_RATIO * max(kept, 1):
            continue
        outer, inner = _join_sides(node)
        if outer is None or inner is None:
            continue
        outer_aliases = {n.alias for n in walk(outer) if n.alias}
        inner_aliases = {n.alias for n in walk(inner) if n.alias}
        if not any(f"{alias}." in masked for alias in outer_aliases) or not any(f"{alias}." in masked for alias in inner_aliases):
            continue
        cost = inclusive_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue

        findings.append(
            PlanFinding(
                issue=PlanIssue.OR_ACROSS_JOIN,
                severity=severity_for_share(share),
                node_type=node.node_type,
                summary=(
                    f"The Nested Loop tested {_fmt(removed + kept)} pairs of rows to keep {_fmt(kept)}: its join condition "
                    "ORs comparisons across the two sides."
                ),
                detail=(
                    f"A hash or merge join needs one equality to match on, and {node.join_filter} offers alternatives, so "
                    "every pair has to be tested. Written as two joins, one for each side of the OR, combined with UNION, "
                    "each half can be hashed or use an index. UNION also removes duplicates the original would keep; "
                    "UNION ALL, with the second half excluding the rows the first matched, keeps them. This is a rewrite "
                    "of the query, so it is a candidate: compare the two plans."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={
                    "join_filter": node.join_filter,
                    "rows_removed_by_join_filter": removed,
                    "rows_returned": kept,
                    "outer": sorted(outer_aliases),
                    "inner": sorted(inner_aliases),
                },
                confidence=Confidence.CANDIDATE,
            )
        )
    return findings


def large_offset(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A LIMIT whose input produced rows only for it to skip them.

    A LIMIT stops pulling rows once it has enough, so its input produces what
    it returns — plus whatever OFFSET told it to skip. Each skipped row was
    read, joined and sorted, then thrown away, and the next page skips more.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        if node.node_type != "Limit":
            continue
        child = _limit_child(node)
        if child is None or child.actual_rows is None or node.actual_rows is None:
            continue
        returned, produced = node.actual_rows, child.actual_rows
        skipped = produced - returned
        if skipped < MIN_SKIPPED_ROWS or skipped < DISCARD_RATIO * max(returned, 1):
            continue
        cost = inclusive_ms(child) * skipped / max(produced, 1)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue
        findings.append(
            PlanFinding(
                issue=PlanIssue.LARGE_OFFSET,
                severity=severity_for_share(share),
                node_type=node.node_type,
                summary=f"The LIMIT skipped {_fmt(skipped)} rows to return {_fmt(returned)}: an OFFSET.",
                detail=(
                    f"Its {child.node_type} produced every skipped row before the LIMIT threw it away, and a later page "
                    "skips more. Keyset pagination remembers the last row's sort key and asks for the rows after it — "
                    "WHERE (key) > (last value) ORDER BY key LIMIT n — so each page costs what the first one did. The "
                    "time shown is the input's, apportioned to the skipped rows."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={"rows_skipped": skipped, "rows_returned": returned, "input_node": child.node_type},
                confidence=Confidence.PROVEN,
            )
        )
    return findings


def cte_materialized(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A CTE computed in full and read once, through a filter it never saw.

    From PostgreSQL 12, a CTE read once is folded into the query, so the
    query's conditions reach its rows. One that appears in the plan as its own
    subplan, read by a single CTE Scan, was kept apart: written MATERIALIZED,
    or calling a volatile function, or modifying data, or recursive. The
    last two show in the plan and are left alone; the first two do not, so
    this is a candidate.
    """
    readers: dict[str, list[PlanNode]] = {}
    for node in walk(root):
        if node.node_type == "CTE Scan" and node.cte_name:
            readers.setdefault(node.cte_name, []).append(node)

    findings: list[PlanFinding] = []
    for cte in walk(root):
        if not cte.subplan_name or not cte.subplan_name.startswith("CTE "):
            continue
        name = cte.subplan_name[len("CTE ") :]
        scans = readers.get(name, [])
        if len(scans) != 1:
            continue
        if any(n.node_type in ("ModifyTable", "Recursive Union", "WorkTable Scan") for n in walk(cte)):
            continue
        scan = scans[0]
        removed, kept = _discards(scan)
        if not removed:
            continue
        cost = inclusive_ms(cte) + self_ms(scan)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue
        findings.append(
            PlanFinding(
                issue=PlanIssue.CTE_MATERIALIZED,
                severity=severity_for_share(share),
                node_type=scan.node_type,
                relation=name,
                summary=(f"CTE {name} was computed in full, {_fmt(cte.actual_rows or 0)} rows, and read once by a scan that kept {_fmt(kept)}."),
                detail=(
                    f"The scan's condition {scan.filter} was applied to the CTE's output, not inside it, where an index could "
                    "have served it. From PostgreSQL 12 a CTE read once is folded into the query unless it is written "
                    "MATERIALIZED or calls a volatile function; written AS NOT MATERIALIZED, it is folded. Check it calls no "
                    "volatile function first, since folding one changes how often it runs."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={"cte": name, "filter": scan.filter, "cte_rows": cte.actual_rows, "rows_removed": removed, "rows_returned": kept},
                remediation=[f"-- WITH {name} AS NOT MATERIALIZED (...)"],
                confidence=Confidence.CANDIDATE,
            )
        )
    return findings


def heap_fetches(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """An Index Only Scan that went to the table for many of its rows.

    It skips the table only on pages the visibility map marks all-visible,
    and VACUUM is what marks them. ``Heap Fetches`` counts the rows it had to
    check in the table anyway — the cost an index-only scan was chosen to
    avoid.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        if node.node_type != "Index Only Scan" or not node.heap_fetches:
            continue
        fetched = node.heap_fetches
        returned = max(node.actual_rows or 0, 0) * loops(node)
        if fetched < MIN_HEAP_FETCHES or fetched < HEAP_FETCH_SHARE * max(returned, 1):
            continue
        fraction = min(fetched / max(returned, 1), 1.0)
        cost = self_ms(node) * fraction
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue
        relation = qualified_relation(node)
        findings.append(
            PlanFinding(
                issue=PlanIssue.HEAP_FETCHES,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=(
                    f"The Index Only Scan{f' on {relation}' if relation else ''} went to the table {_fmt(fetched)} times for {_fmt(returned)} rows."
                ),
                detail=(
                    "An index-only scan skips the table only on pages the visibility map marks all-visible, and VACUUM is "
                    "what marks them. These pages have changed since the table was last vacuumed. The time shown is the "
                    "scan's, apportioned to the rows that went to the table. If it recurs, autovacuum is not reaching the "
                    "table often enough: lower its autovacuum_vacuum_scale_factor, or for a table that is only inserted "
                    "into, autovacuum_vacuum_insert_scale_factor."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={"heap_fetches": fetched, "rows_returned": returned, "index": node.index_name},
                remediation=[f"VACUUM (ANALYZE) {_quoted_relation(relation)};"] if relation else [],
                confidence=Confidence.PROVEN,
            )
        )
    return findings


def sparse_heap(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """A sequential scan that read far more pages than its rows can fill.

    Needs ``BUFFERS``. A sequential scan reads every page of the table and
    tests every live row on it, so pages read against rows returned and
    removed is the table's density. Fewer than four live rows a page means
    most of each page is dead rows or free space. Inferred from a count, so
    a candidate, with ``pgstattuple`` to confirm it.
    """
    findings: list[PlanFinding] = []
    for node in walk(root):
        if node.node_type != "Seq Scan" or node.shared_hit_blocks is None:
            continue
        pages = (node.shared_hit_blocks or 0) + (node.shared_read_blocks or 0)
        if pages < MIN_SCANNED_PAGES:
            continue
        rows = ((node.actual_rows or 0) + (node.rows_removed_by_filter or 0)) * loops(node)
        per_page = rows / pages
        if per_page >= SPARSE_ROWS_PER_PAGE:
            continue
        cost = self_ms(node)
        share = context.share(cost)
        if share < MIN_REPORTABLE_SHARE:
            continue
        relation = qualified_relation(node)
        name = relation or node.relation_name or "the table"
        remediation = (
            [
                f"SELECT * FROM pgstattuple('{_quoted_relation(relation)}');  -- confirm: dead_tuple_percent and free_percent",
                f"VACUUM {_quoted_relation(relation)};  -- makes the dead rows' space reusable; does not shrink the table",
                "-- To shrink it: VACUUM FULL, which blocks reads and writes while it rewrites the table, or pg_repack, which does not.",
            ]
            if relation
            else []
        )
        findings.append(
            PlanFinding(
                issue=PlanIssue.SPARSE_HEAP,
                severity=severity_for_share(share),
                node_type=node.node_type,
                relation=relation,
                summary=f"The Seq Scan on {name} read {_fmt(pages)} pages for {_fmt(rows)} live rows, {per_page:.1f} a page.",
                detail=(
                    "A page is 8 kB, and a row larger than about 2 kB is moved out of line to TOAST, so a page of live rows "
                    "holds at least four. Fewer means the pages are mostly dead rows or free space left by deletes and "
                    "updates, and the scan read them all. This is inferred from the page count, so confirm it before acting."
                ),
                cost_ms=cost,
                share_of_time=share,
                evidence={"pages_read": pages, "live_rows": rows, "rows_per_page": round(per_page, 2)},
                remediation=remediation,
                confidence=Confidence.CANDIDATE,
            )
        )
    return findings


# -- Time no node includes ------------------------------------------------------


def trigger_time(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """Triggers that took a large share of the execution.

    No plan node's time includes a trigger's, so a statement that spends most
    of its time in triggers looks fast node by node. A referential-integrity
    trigger enforcing a foreign key is the usual one: deleting a referenced
    row looks up the rows that reference it, and without an index on the
    referencing columns each lookup scans that table.
    """
    execution = context.execution_ms
    if not execution:
        return []
    findings: list[PlanFinding] = []
    for trigger in context.triggers:
        ms = float(trigger.get("Time") or 0.0)
        share = ms / execution
        if ms < MIN_UNATTRIBUTED_MS or share < MIN_UNATTRIBUTED_SHARE:
            continue
        name = str(trigger.get("Trigger Name") or "")
        relation = trigger.get("Relation")
        calls = int(trigger.get("Calls") or 0)
        constraint = trigger.get("Constraint Name")

        detail = f"No plan node's time includes it, so the plan's nodes account for {_ms(context.total_ms)} of the {_ms(execution)}."
        remediation: list[str] = []
        confidence: Confidence | None = None
        evidence: dict[str, Any] = {
            "trigger": name,
            "relation": relation,
            "calls": calls,
            "time_ms": ms,
            "execution_ms": execution,
            "share_of": "execution_time",
        }
        if constraint:
            evidence["constraint"] = constraint
            keys = [fk for fk in context.foreign_keys.get(str(constraint), []) if relation in (_table_of(fk.referenced), _table_of(fk.referencing))]
            if keys:
                key = keys[0]
                columns = ", ".join(key.columns)
                evidence.update({"referencing": key.referencing, "columns": list(key.columns), "indexed": key.indexed})
                if _table_of(key.referenced) == relation and not key.indexed:
                    detail += (
                        f" It enforces foreign key {constraint}: each change to a {key.referenced} row looks up the "
                        f"{key.referencing} rows that reference it, and no index on {key.referencing} leads with "
                        f"({columns}), so each of the {_fmt(calls)} calls scanned the table."
                    )
                    remediation.append(f"CREATE INDEX CONCURRENTLY ON {_quoted_relation(key.referencing)} ({columns});")
                    confidence = Confidence.PROVEN
                elif _table_of(key.referenced) == relation:
                    detail += (
                        f" It enforces foreign key {constraint}, and an index on {key.referencing} ({columns}) exists, so "
                        "the time is the referencing rows' own work: the cascade changing them, and their triggers."
                    )
                else:
                    detail += f" It checks each changed {key.referencing} row against {key.referenced}, for foreign key {constraint}."
            elif not context.catalog:
                detail += f" It enforces constraint {constraint}; whether its referencing columns are indexed needs the catalog."

        findings.append(
            PlanFinding(
                issue=PlanIssue.TRIGGER_TIME,
                severity=severity_for_share(share),
                node_type="Trigger",
                relation=str(relation) if relation else None,
                summary=f"Trigger {name} took {_ms(ms)} over {_fmt(calls)} calls, {share:.0%} of the execution time.",
                detail=detail,
                cost_ms=ms,
                share_of_time=share,
                evidence=evidence,
                remediation=remediation,
                confidence=confidence,
            )
        )
    return findings


def _table_of(relation: str) -> str:
    return relation.rpartition(".")[2]


def jit_time(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """JIT compilation that took a large share of a short query.

    JIT pays for itself on a query that evaluates the same expressions over
    many rows, and costs more than it saves on a short one. It is used when
    the plan's cost passes ``jit_above_cost``, and the cost is an estimate
    of work, not of time, so a cheap-looking query can cross it.
    """
    jit = context.jit
    execution = context.execution_ms
    if not jit or not execution:
        return []
    timing = jit.get("Timing")
    if not isinstance(timing, dict):
        return []
    total = timing.get("Total")
    if not isinstance(total, (int, float)):
        return []
    share = total / execution
    if total < MIN_UNATTRIBUTED_MS or share < MIN_UNATTRIBUTED_SHARE:
        return []

    options = jit.get("Options")
    if not isinstance(options, dict):
        options = {}
    phases = ", ".join(f"{phase.lower()} {_ms(float(timing[phase]))}" for phase in ("Inlining", "Optimization", "Emission") if timing.get(phase))
    # Above this plan's cost, and never below PostgreSQL's own default: a
    # threshold lowered from it is the likelier cause than the default.
    threshold = max(10 ** math.ceil(math.log10(max(root.total_cost, 1.0) + 1)), DEFAULT_JIT_ABOVE_COST)
    current = context.settings.get("jit_above_cost")
    detail = (
        f"JIT compiled {_fmt(int(jit.get('Functions') or 0))} functions in {_ms(total)}"
        f"{f' ({phases})' if phases else ''}, {share:.0%} of the {_ms(execution)} the query ran. JIT is used when a plan's "
        f"estimated cost passes jit_above_cost{f' ({current} here)' if current else ''}, and this plan's is "
        f"{root.total_cost:,.0f}: an estimate of work that crossed the threshold on a query too short to repay the compiling."
    )
    remediation = [f"SET jit_above_cost = {threshold};  -- above this plan's cost; or SET jit = off for short queries"]
    if options.get("Inlining") or options.get("Optimization"):
        detail += " Inlining and optimization are the expensive phases, and have thresholds of their own."
        remediation.append(f"SET jit_inline_above_cost = {threshold}; SET jit_optimize_above_cost = {threshold};")
    return [
        PlanFinding(
            issue=PlanIssue.JIT_TIME,
            severity=severity_for_share(share),
            node_type=root.node_type,
            summary=f"JIT compilation took {_ms(total)}, {share:.0%} of the execution time.",
            detail=detail,
            cost_ms=float(total),
            share_of_time=share,
            evidence={
                "jit_ms": total,
                "timing": timing,
                "options": options,
                "functions": jit.get("Functions"),
                "plan_cost": root.total_cost,
                "jit_above_cost": current,
                "execution_ms": execution,
                "share_of": "execution_time",
            },
            remediation=remediation,
            confidence=Confidence.PROVEN,
        )
    ]


def planning_time(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """Planning that took longer than running the query.

    Planning time grows with the number of join orders the planner weighs —
    exponentially, up to ``join_collapse_limit`` relations — and with the
    partitions it has to consider. A statement planned on every execution
    pays it every time; a prepared one pays it once per connection.
    """
    planning, execution = context.planning_ms, context.execution_ms
    if planning is None or execution is None or planning < MIN_UNATTRIBUTED_MS or planning < execution:
        return []
    share = planning / (planning + execution)
    relations = sum(1 for node in walk(root) if node.relation_name)
    joins = sum(1 for node in walk(root) if node.node_type in ("Nested Loop", "Hash Join", "Merge Join"))
    if relations < MIN_PLANNED_RELATIONS:
        # One relation leaves no join order to weigh and no partitions to
        # consider. What is left is the first statement on a connection
        # loading the catalog caches, which is not the query's to fix.
        return []
    return [
        PlanFinding(
            issue=PlanIssue.PLANNING_TIME,
            severity=severity_for_share(share),
            node_type=root.node_type,
            summary=f"Planning took {_ms(planning)}, longer than the {_ms(execution)} the query ran.",
            detail=(
                f"The plan reads {_fmt(relations)} relations through {_fmt(joins)} joins. The join orders the planner "
                "weighs grow exponentially with the relations joined, up to join_collapse_limit, and every partition "
                "not pruned is another relation. Preparing the statement plans it once per connection — behind a "
                "transaction pooler, only one that supports protocol-level prepared statements, such as PgBouncer 1.21 "
                "and later with max_prepared_statements. The first statement on a new connection also pays for loading "
                "the catalog caches, so run it twice before acting on this."
            ),
            cost_ms=planning,
            share_of_time=share,
            evidence={
                "planning_ms": planning,
                "execution_ms": execution,
                "relations": relations,
                "joins": joins,
                "share_of": "planning_and_execution_time",
            },
            confidence=Confidence.PROVEN,
        )
    ]


#: Every rule, in the order they are run. Findings are sorted afterwards, so
#: this order only decides ties.
RULES = (
    row_estimate,
    stale_statistics,
    correlated_columns,
    generic_plan,
    nested_loop_blowup,
    rls_per_row_call,
    rls_leaky_predicate,
    discarded_rows,
    limit_scan_filter,
    function_on_indexed_column,
    or_across_join,
    large_offset,
    cte_materialized,
    heap_fetches,
    sparse_heap,
    sort_spilled,
    hash_spilled,
    lossy_bitmap,
    trigger_time,
    jit_time,
    planning_time,
    workers_not_launched,
)


def _join_sides(node: PlanNode) -> tuple[PlanNode | None, PlanNode | None]:
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
    "HEAP_FETCH_SHARE",
    "MIN_DISCARDED_ROWS",
    "MIN_HEAP_FETCHES",
    "MIN_LOOP_ITERATIONS",
    "MIN_PLANNED_RELATIONS",
    "MIN_SCANNED_PAGES",
    "MIN_SKIPPED_ROWS",
    "MIN_UNATTRIBUTED_MS",
    "MIN_UNATTRIBUTED_SHARE",
    "RULES",
    "SPARSE_ROWS_PER_PAGE",
    "RuleContext",
    "correlated_columns",
    "cte_materialized",
    "discarded_rows",
    "function_on_indexed_column",
    "generic_plan",
    "hash_spilled",
    "heap_fetches",
    "jit_time",
    "large_offset",
    "limit_scan_filter",
    "lossy_bitmap",
    "nested_loop_blowup",
    "or_across_join",
    "planning_time",
    "rls_leaky_predicate",
    "rls_per_row_call",
    "row_estimate",
    "sort_spilled",
    "sparse_heap",
    "stale_statistics",
    "trigger_time",
    "workers_not_launched",
]
