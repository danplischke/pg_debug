"""The vocabulary a plan diagnosis is expressed in.

Every finding here is anchored to one node of one execution plan, and carries
the share of the query's work that flowed through that node. That share is what
makes the finding worth acting on or not: a sort that spilled to disk matters
if it is most of the query and does not if it is two percent of it, and only
the measurement says which.

There is deliberately no rule selector here, unlike
:class:`~pg_debug.model.RuleId`. A model rule costs a query against the
catalog, so choosing between them saves real work; these all read a tree that
has already been fetched, so running every one of them is free and selecting
between them would be surface with nothing behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any

from ..severity import SEVERITY_ORDER
from ..severity import Confidence
from ..severity import Severity

#: How far an estimate has to be from the truth before it is worth reporting.
#: Below roughly this, the planner's choice of plan rarely changes.
DEFAULT_MIN_RATIO = 10.0

#: Ignore a misestimate on fewer rows than this however wrong it is
#: proportionally. One row estimated where twenty were found is a ratio of
#: twenty and costs nothing.
MIN_MISESTIMATED_ROWS = 100

#: A node has to account for at least this share of the query's work before a
#: rule that reports a cost — a spill, a discarded scan — bothers to mention it.
MIN_REPORTABLE_SHARE = 0.01


class PlanIssue(str, Enum):
    """What a diagnosis can find in a plan."""

    #: The lowest node whose row estimate diverges from what it found. Every
    #: other estimate above it in the tree inherited this one's error.
    ROW_ESTIMATE = "row_estimate"
    #: That node reads a table whose statistics no longer describe it.
    STALE_STATISTICS = "stale_statistics"
    #: That node restricts on several columns of one table that are correlated,
    #: which the planner cannot know without extended statistics.
    CORRELATED_COLUMNS = "correlated_columns"
    #: A nested loop re-executing its inner side far more often than the plan
    #: expected to.
    NESTED_LOOP_BLOWUP = "nested_loop_blowup"
    #: A scan reading rows in order to throw them away.
    DISCARDED_ROWS = "discarded_rows"
    #: A sort that did not fit in work_mem.
    SORT_SPILLED = "sort_spilled"
    #: A hash that did not fit in work_mem and was split into batches.
    HASH_SPILLED = "hash_spilled"
    #: A bitmap that did not fit in work_mem and degraded to page granularity.
    LOSSY_BITMAP = "lossy_bitmap"
    #: Parallel workers were planned for and not granted.
    WORKERS_NOT_LAUNCHED = "workers_not_launched"
    #: An Append over many partitions of one table, every one of them scanned
    #: and filtered on the partition key, most of them for no rows: pruning
    #: could not use the condition. Inferred, so a candidate.
    UNPRUNED_PARTITIONS = "unpruned_partitions"
    #: A misestimated node whose condition compares with a ``$n`` parameter:
    #: a generic plan, estimated from the column's average rather than the value.
    GENERIC_PLAN = "generic_plan"
    #: A row-level security policy calling a STABLE function once per row,
    #: where ``(select fn())`` would call it once per statement.
    RLS_PER_ROW_CALL = "rls_per_row_call"
    #: A predicate under row-level security whose operator or function is not
    #: LEAKPROOF, so it cannot use the index that exists for it.
    RLS_LEAKY_PREDICATE = "rls_leaky_predicate"
    #: A LIMIT over an index scan that walked the index in order and discarded
    #: most of what it read before finding enough rows.
    LIMIT_SCAN_FILTER = "limit_scan_filter"
    #: A filter applying a function to a column an index exists on, which the
    #: index cannot serve.
    FUNCTION_ON_INDEXED_COLUMN = "function_on_indexed_column"
    #: A join condition that ORs comparisons across the two sides, so the join
    #: can only compare every pair.
    OR_ACROSS_JOIN = "or_across_join"
    #: A LIMIT that produced rows only to skip them: a large OFFSET.
    LARGE_OFFSET = "large_offset"
    #: A CTE computed in full and read once through a filter it was not given.
    CTE_MATERIALIZED = "cte_materialized"
    #: An Index Only Scan that went to the heap for many of its rows.
    HEAP_FETCHES = "heap_fetches"
    #: A sequential scan that read far more pages than its rows can fill.
    SPARSE_HEAP = "sparse_heap"
    #: Trigger time, which no plan node includes.
    TRIGGER_TIME = "trigger_time"
    #: Time spent compiling the query with JIT.
    JIT_TIME = "jit_time"
    #: Time spent planning the query, against the time spent running it.
    PLANNING_TIME = "planning_time"


#: Reading order for findings that matter equally. A wrong row estimate is the
#: observation; stale statistics and correlated columns are the two things that
#: explain it, so they follow it rather than sorting away from it alphabetically.
ISSUE_ORDER = {
    PlanIssue.ROW_ESTIMATE: 0,
    PlanIssue.STALE_STATISTICS: 1,
    PlanIssue.CORRELATED_COLUMNS: 2,
    PlanIssue.GENERIC_PLAN: 3,
    PlanIssue.NESTED_LOOP_BLOWUP: 4,
    PlanIssue.RLS_PER_ROW_CALL: 5,
    PlanIssue.RLS_LEAKY_PREDICATE: 6,
    PlanIssue.DISCARDED_ROWS: 7,
    PlanIssue.LIMIT_SCAN_FILTER: 8,
    PlanIssue.FUNCTION_ON_INDEXED_COLUMN: 9,
    PlanIssue.OR_ACROSS_JOIN: 10,
    PlanIssue.LARGE_OFFSET: 11,
    PlanIssue.CTE_MATERIALIZED: 12,
    PlanIssue.HEAP_FETCHES: 13,
    PlanIssue.SPARSE_HEAP: 14,
    PlanIssue.SORT_SPILLED: 15,
    PlanIssue.HASH_SPILLED: 16,
    PlanIssue.LOSSY_BITMAP: 17,
    PlanIssue.TRIGGER_TIME: 18,
    PlanIssue.JIT_TIME: 19,
    PlanIssue.PLANNING_TIME: 20,
    PlanIssue.UNPRUNED_PARTITIONS: 21,
    PlanIssue.WORKERS_NOT_LAUNCHED: 22,
}

#: Findings about a choice the planner made, which a user may want to pin
#: while the cause is fixed. Each carries the plan-control mechanisms the
#: server offers, as advice.
PLAN_CHOICE_ISSUES = frozenset(
    {
        PlanIssue.ROW_ESTIMATE,
        PlanIssue.GENERIC_PLAN,
        PlanIssue.NESTED_LOOP_BLOWUP,
        PlanIssue.LIMIT_SCAN_FILTER,
    }
)


def severity_for_share(share: float) -> Severity:
    """How much a finding matters, from the share of work behind it.

    Severity is measured rather than assigned, for the same reason the rest of
    the tool argues from counters: a rule that calls its own findings important
    is not evidence of anything.
    """
    if share >= 0.5:
        return Severity.HIGH
    if share >= 0.15:
        return Severity.MEDIUM
    return Severity.LOW


@dataclass
class PlanFinding:
    """One thing wrong with an execution plan, and the node it happened at.

    Attributes:
        issue: The :class:`PlanIssue` that produced it.
        severity: How much it matters, derived from ``share_of_time``.
        node_type: The plan node, as PostgreSQL names it — ``Seq Scan``,
            ``Nested Loop``.
        relation: The table the node reads, schema-qualified, or ``None`` for a
            node that reads no table of its own such as a Sort or a Hash.
        summary: One sentence, enough on its own.
        detail: Why it matters here, in this plan's numbers.
        cost_ms: Milliseconds attributable to the finding. Which region of the
            plan that covers is the rule's judgement, not a fixed choice: a
            spilled sort costs what that node itself cost, while a bad row
            estimate costs whatever the subtree it misled cost, which is the
            whole point of reporting it.
        share_of_time: ``cost_ms`` as a share of the query's total work. For a
            plan with parallel workers this is a share of total work rather
            than of elapsed time, because a node run by three workers did three
            nodes' worth of work in one node's worth of wall clock. Time no
            node includes — triggers, JIT compilation — is a share of the
            execution time, and planning a share of planning plus execution;
            ``evidence["share_of"]`` says which when it is not the nodes'.
        evidence: The measurements behind the verdict.
        remediation: Statements or settings that address it. Empty where the
            fix is a decision rather than a statement.
        confidence: For the kinds of finding that infer as well as read:
            ``proven`` when the plan and the catalog show it, ``candidate``
            when it rests on reading a condition's text or on what pg-debug
            could not check. None for a finding that is only a measurement.
        plan_control: For a finding about a choice the planner made, the
            mechanisms this server offers to pin a plan while the cause is
            fixed. Advice only: pg-debug never applies one.
    """

    issue: PlanIssue
    severity: Severity
    node_type: str
    summary: str
    detail: str
    cost_ms: float = 0.0
    share_of_time: float = 0.0
    relation: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)
    confidence: Confidence | None = None
    plan_control: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple[int, float, int]:
        """Severity, then the share of the query, then reading order."""
        return (SEVERITY_ORDER[self.severity], -self.share_of_time, ISSUE_ORDER[self.issue])

    def to_dict(self) -> dict[str, Any]:
        """Return the finding as a JSON-friendly dict."""
        result: dict[str, Any] = {
            "issue": self.issue.value,
            "severity": self.severity.value,
            "node_type": self.node_type,
            "summary": self.summary,
            "detail": self.detail,
            "cost_ms": round(self.cost_ms, 3),
            "share_of_time": round(self.share_of_time, 4),
            "evidence": self.evidence,
            "remediation": self.remediation,
        }
        if self.relation is not None:
            result["relation"] = self.relation
        if self.confidence is not None:
            result["confidence"] = self.confidence.value
        if self.plan_control:
            result["plan_control"] = self.plan_control
        return result


__all__ = [
    "DEFAULT_MIN_RATIO",
    "ISSUE_ORDER",
    "MIN_MISESTIMATED_ROWS",
    "MIN_REPORTABLE_SHARE",
    "PLAN_CHOICE_ISSUES",
    "PlanFinding",
    "PlanIssue",
    "severity_for_share",
]
