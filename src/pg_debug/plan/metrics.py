"""The arithmetic a plan diagnosis rests on.

Two numbers decide almost everything a rule says, and both are easy to get
wrong by reading the plan literally.

**Time is per loop and includes children.** ``Actual Total Time`` is the time
for *one* execution of a node, and it contains the time of everything beneath
it. So a node's own cost is
``actual_total_time * actual_loops - Σ(child inclusive time)``. Skip either
half of that and the root node comes out as 100% of the query every time, which
says nothing: of course the whole plan takes the whole time. What is wanted is
which *node* the time went into.

**A misestimate propagates upwards.** A scan that expected 41 rows and found
4000 makes every join above it wrong too, so a rule that reports each diverging
node buries the cause under a list of its own consequences. Only the lowest
diverging node is the cause, and :func:`lowest_misestimates` finds it.

Everything here is a pure function of a :class:`~pg_debug.artifacts.PlanNode`
tree — no database, no I/O — so the arithmetic that decides every verdict is
testable without a server.
"""

from __future__ import annotations

from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple

from ..artifacts import PlanNode
from .result import MIN_MISESTIMATED_ROWS


def walk(node: PlanNode) -> Iterator[PlanNode]:
    """Yield every node in the tree, parents before children."""
    yield node
    for child in node.children:
        yield from walk(child)


def walk_with_parent(node: PlanNode, parent: Optional[PlanNode] = None) -> Iterator[Tuple[PlanNode, Optional[PlanNode]]]:
    """Yield ``(node, parent)`` for every node, the root paired with ``None``."""
    yield node, parent
    for child in node.children:
        yield from walk_with_parent(child, node)


def loops(node: PlanNode) -> int:
    """How many times the node ran. One, when the plan carries no timings."""
    return max(node.actual_loops or 1, 1)


def inclusive_ms(node: PlanNode) -> float:
    """Total time in this node and everything below it, across all its loops."""
    if node.actual_total_time is None:
        return 0.0
    return node.actual_total_time * loops(node)


def self_ms(node: PlanNode) -> float:
    """Time in this node alone, its children's time removed.

    Clamped at zero rather than allowed to go negative. It goes negative at a
    Gather, whose children report the summed work of several processes while
    the Gather itself reports wall clock — the parallelism is real, and the
    node did not somehow take less than no time.
    """
    if node.actual_total_time is None:
        return 0.0
    return max(inclusive_ms(node) - sum(inclusive_ms(child) for child in node.children), 0.0)


def total_self_ms(root: PlanNode) -> float:
    """The whole tree's self time, which is what shares are taken against.

    Not ``Execution Time``: for a parallel plan the workers' time is real work
    that a wall clock does not count twice, and dividing by the wall clock
    would produce shares summing to well over one. Against this total they sum
    to exactly one, and the docstring on
    :attr:`~pg_debug.plan.result.PlanFinding.share_of_time` says what that
    means.
    """
    return sum(self_ms(node) for node in walk(root))


def shares(root: PlanNode) -> Dict[int, float]:
    """Each node's share of the query's work, keyed by :func:`id`."""
    total = total_self_ms(root)
    if total <= 0:
        return {id(node): 0.0 for node in walk(root)}
    return {id(node): self_ms(node) / total for node in walk(root)}


def estimate_ratio(node: PlanNode) -> float:
    """How far the row estimate was out, as a factor at least 1.

    Symmetric: estimating 4000 where 40 were found is as wrong as the reverse,
    and both mislead the planner. Returns 1.0 — no divergence — for a plan with
    no ANALYZE, where there is nothing to compare against.
    """
    if node.actual_rows is None:
        return 1.0
    estimated = max(node.plan_rows, 0)
    actual = max(node.actual_rows, 0)
    return max(estimated, actual) / max(min(estimated, actual), 1)


def is_misestimated(node: PlanNode, min_ratio: float) -> bool:
    """Whether the node's estimate is both proportionally and absolutely wrong.

    The absolute floor is what keeps the report readable. One row estimated
    where twenty were found is a factor of twenty and costs nothing, and a plan
    is full of those.
    """
    if node.actual_rows is None:
        return False
    if max(node.plan_rows, node.actual_rows) < MIN_MISESTIMATED_ROWS:
        return False
    return estimate_ratio(node) >= min_ratio


def lowest_misestimates(root: PlanNode, min_ratio: float) -> List[PlanNode]:
    """The diverging nodes that have no diverging node beneath them.

    These are the causes. Everything above them in the tree is being fed a
    wrong row count and cannot be blamed for believing it.
    """
    found: List[PlanNode] = []

    def visit(node: PlanNode) -> bool:
        """Return whether this subtree contains a misestimate."""
        below = [visit(child) for child in node.children]
        if any(below):
            return True
        if is_misestimated(node, min_ratio):
            found.append(node)
            return True
        return False

    visit(root)
    return found


def qualified_relation(node: PlanNode) -> Optional[str]:
    """``schema.table`` for a node that reads one, else ``None``.

    The schema is only there when the plan was taken with VERBOSE. Without it
    the bare name is returned, which is what the plan itself shows.
    """
    if not node.relation_name:
        return None
    if node.schema_name:
        return f"{node.schema_name}.{node.relation_name}"
    return node.relation_name


def conditions(node: PlanNode) -> List[str]:
    """Every predicate the node applies, whichever slot the plan put it in."""
    return [text for text in (node.filter, node.index_cond, node.recheck_cond, node.join_filter, node.hash_cond) if text]


__all__ = [
    "conditions",
    "estimate_ratio",
    "inclusive_ms",
    "is_misestimated",
    "loops",
    "lowest_misestimates",
    "qualified_relation",
    "self_ms",
    "shares",
    "total_self_ms",
    "walk",
    "walk_with_parent",
]
