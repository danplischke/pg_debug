"""Plan shapes: what a query's plan does, without what it costs, for a build to watch.

A migration that drops an index, a change that makes a predicate
non-sargable, statistics that tip the planner over: each turns an index scan
into a sequential scan, and nothing fails until production is slow. A plan
shape is the part of ``EXPLAIN`` that says what the plan does — the node
types, how they join and aggregate, which relations and indexes they read —
and none of the numbers. Costs and row estimates move with every ``ANALYZE``
and would fail a build on noise; the shape changes only when the plan does.

``EXPLAIN`` without ``ANALYZE`` runs nothing, so this is safe in restricted
mode. A query with ``$1`` parameters is planned as the explain command plans
it: a generic plan on PostgreSQL 16 and later, sample values before.

The planner decides from the database it is asked on. A CI database with
empty tables plans differently from production, and the shapes it records
are its own: compare a CI run against a baseline from CI, seeded the same way.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pglast import parser as pg_parser
from pglast import split

from ..definitions import fingerprint
from ..errors import InvalidArgumentError
from ..errors import PgDebugError
from .explain import ExplainPlanTool

#: The plan fields that make up a node's shape, in the order they are written.
#: Everything else a plan node carries is a number (costs, rows, widths,
#: workers) or an expression whose constants differ from run to run.
_SHAPE_FIELDS = (
    ("Join Type", "{}"),
    ("Strategy", "{}"),
    ("Partial Mode", "{}"),
    ("Operation", "{}"),
    ("Scan Direction", "{}"),
    ("Relation Name", "on {}"),
    ("Function Name", "on {}()"),
    ("CTE Name", "on CTE {}"),
    ("Index Name", "using {}"),
    ("Subplan Name", "({})"),
)

#: A leading ``-- name: ...`` comment names a query in the baseline and the
#: report instead of its fingerprint.
_NAME = re.compile(r"^\s*--\s*name:\s*(\S.*?)\s*$", re.MULTILINE)


def shape_lines(plan: Mapping[str, Any]) -> list[str]:
    """A plan's shape, one line per node, children indented under their parent.

    Accepts the document ``EXPLAIN (FORMAT JSON)`` returns, or its ``Plan``.
    """
    root = plan.get("Plan", plan)
    lines: list[str] = []
    _walk(root, 0, lines)
    return lines


def _walk(node: Mapping[str, Any], depth: int, lines: list[str]) -> None:
    parts = []
    if node.get("Parent Relationship") in ("InitPlan", "SubPlan"):
        parts.append(f"[{node['Parent Relationship']}]")
    node_type = str(node.get("Node Type", "?"))
    parts.append(f"Parallel {node_type}" if node.get("Parallel Aware") else node_type)
    for name, form in _SHAPE_FIELDS:
        value = node.get(name)
        # A forward scan is every scan; only a backward one says something.
        if value is None or (name == "Scan Direction" and value == "Forward"):
            continue
        parts.append(form.format(value))
    lines.append("  " * depth + " ".join(parts))
    for child in node.get("Plans") or []:
        _walk(child, depth + 1, lines)


def shape_fingerprint(lines: Iterable[str]) -> str:
    """The short hash a baseline records for a shape."""
    return fingerprint("\n".join(lines))


@dataclass
class PlanShape:
    """One query's plan shape.

    Attributes:
        query: The query as given.
        name: What it is known by: its ``-- name:`` comment, or else its
            fingerprint, which ignores constants, whitespace and comments.
        shape: One line per plan node, indented under its parent.
        fingerprint: The hash of ``shape`` a baseline records; None when the
            query could not be planned.
        error: Why the query could not be planned, when it could not.
    """

    query: str
    name: str
    shape: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    error: str | None = None

    @property
    def about(self) -> str:
        """The query on one line, cut short, to recognise it by."""
        text = " ".join(_NAME.sub("", self.query).split())
        return text if len(text) <= 80 else text[:79] + "…"

    def key(self) -> dict[str, Any]:
        """What a baseline records: the query's name, and its shape as fingerprint and lines."""
        return {"query": self.name, "about": self.about, "fingerprint": self.fingerprint, "shape": list(self.shape)}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "query": self.query, "fingerprint": self.fingerprint, "shape": self.shape}
        if self.error is not None:
            payload["error"] = self.error
        return payload


def split_queries(text: str) -> list[tuple[str, str]]:
    """Each statement of a file of queries, with the name it is known by.

    Raises:
        InvalidArgumentError: If the text does not parse, holds no statement,
            or two statements have the same name: one of them would be
            compared against the other's shape.
    """
    try:
        statements = [s.strip() for s in split(text, with_parser=True) if s.strip()]
    except Exception as e:
        raise InvalidArgumentError(f"Could not split the queries into statements: {e}") from e
    if not statements:
        raise InvalidArgumentError("No query to plan: the file holds no statement.")

    named: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for statement in statements:
        match = _NAME.search(statement.split("\n", 1)[0]) if statement.lstrip().startswith("--") else None
        name = match.group(1) if match else pg_parser.fingerprint(statement)
        if name in seen:
            raise InvalidArgumentError(
                f"Two queries are both known as {name}: {_one_line(seen[name])} and {_one_line(statement)}. Queries that differ only "
                "in constants share a fingerprint; give each its own with a '-- name: ...' line above it."
            )
        seen[name] = statement
        named.append((name, statement))
    return named


def _one_line(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 60 else flat[:59] + "…"


async def read_plan_shapes(tool: ExplainPlanTool, queries: Iterable[str]) -> list[PlanShape]:
    """Plan each query without running it and read its shape.

    A query that cannot be planned is returned with ``error`` set, and the
    others are still planned.
    """
    shapes: list[PlanShape] = []
    # One text, so that a name is unique across every file given.
    for name, statement in split_queries("\n;\n".join(queries)):
        try:
            plan = await tool.explain(statement)
            lines = shape_lines(json.loads(plan.value))
        except PgDebugError as e:
            shapes.append(PlanShape(query=statement, name=name, error=str(e)))
            continue
        shapes.append(PlanShape(query=statement, name=name, shape=lines, fingerprint=shape_fingerprint(lines)))
    return shapes


__all__ = ["PlanShape", "read_plan_shapes", "shape_fingerprint", "shape_lines", "split_queries"]
