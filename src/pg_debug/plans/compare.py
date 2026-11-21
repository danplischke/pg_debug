"""Several ways of writing a query, or one query with several sets of values, run side by side.

The question "is this rewrite faster" is answered by running both, and "is
this query slow for every customer or only for the big one" by running it with
each one's values. Each combination is run under ``EXPLAIN (ANALYZE,
BUFFERS)``, as many times as asked, and reported with its median time, what it
read, the rows it returned and its plan's shape, and the last run is diagnosed
as ``diagnose`` would.

What is reported is what can be measured, and what the measurement cannot
settle is said: a first run pays for reading pages a later run finds in the
cache; two variants that return different numbers of rows are not the same
query; one variant whose plan changes with the values is a plan the planner
chose per value, and a prepared statement may not get it.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pglast import parse_sql
from pglast.visitors import Visitor

from ..errors import ExplainPlanError
from ..errors import InvalidArgumentError
from .result import PlanFinding
from .shape import shape_fingerprint
from .shape import shape_lines

#: How many times each combination may be run.
MAX_REPEAT = 20

#: How many combinations of variant and values one comparison may run.
MAX_RUNS = 50


@dataclass
class QueryRun:
    """One variant with one set of values, run and measured.

    Attributes:
        label: ``A``, ``B``... for the variant, with the values when there
            are several sets: ``A (42, 'open')``.
        variant: Which variant, counting from 0.
        values: The values its ``$1``, ``$2``... were given, or None.
        query: The statement as run, the values written in.
        execution_ms: The median execution time of its runs.
        runs_ms: Each run's execution time, in order: the first pays for
            pages a later one finds in the cache.
        planning_ms: The last run's planning time.
        rows: The rows the statement returned (or wrote), from the plan's top node.
        shared_hit: Shared buffers found in the cache, last run, whole plan.
        shared_read: Shared buffers read from disk or the OS cache.
        temp_written: Temporary blocks written: a sort or hash past work_mem.
        shape: The plan's shape, one line per node, as ``plans`` records it.
        fingerprint: The shape's hash: two runs with the same plan share it.
        findings: What ``diagnose`` finds wrong with the last run's plan.
        error: Why it could not be run, when it could not; the rest is empty then.
    """

    label: str
    variant: int
    values: list[Any] | None
    query: str
    execution_ms: float | None = None
    runs_ms: list[float] = field(default_factory=list)
    planning_ms: float | None = None
    rows: int | None = None
    shared_hit: int | None = None
    shared_read: int | None = None
    temp_written: int | None = None
    shape: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    findings: list[PlanFinding] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "variant": self.variant,
            "values": self.values,
            "query": self.query,
            "execution_ms": self.execution_ms,
            "runs_ms": self.runs_ms,
            "planning_ms": self.planning_ms,
            "rows": self.rows,
            "shared_hit": self.shared_hit,
            "shared_read": self.shared_read,
            "temp_written": self.temp_written,
            "shape": self.shape,
            "fingerprint": self.fingerprint,
            "findings": [finding.to_dict() for finding in self.findings],
            "error": self.error,
        }


@dataclass
class QueryComparison:
    """The runs of a comparison, and what they say together.

    Attributes:
        runs: Each variant with each set of values, in the order given.
        fastest: The label of the run with the lowest median time.
        notes: What the comparison cannot settle, or found along the way:
            variants that returned different numbers of rows, a variant whose
            plan changed with its values, a first run slower than the rest.
    """

    runs: list[QueryRun]
    fastest: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"runs": [run.to_dict() for run in self.runs], "fastest": self.fastest, "notes": self.notes}


class _Params(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[int, int]] = []

    def visit_ParamRef(self, ancestors: Any, node: Any) -> None:  # noqa: N802 (pglast's naming)
        self.found.append((node.number, node.location))


def literal(value: Any) -> str:
    """A value as an SQL literal: a number bare, a string quoted, None as NULL.

    A string is left untyped, as a bind parameter is, so ``'42'`` compares
    with an integer column and ``'2026-01-01'`` with a date.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple, dict)):
        value = json.dumps(value)
    return "'" + str(value).replace("'", "''") + "'"


def bind(sql: str, values: Sequence[Any]) -> str:
    """The statement with ``$1``, ``$2``... replaced by these values, as literals.

    Only parameters are replaced: a ``$1`` inside a string or a comment is
    text, and stays.

    Raises:
        InvalidArgumentError: If the statement does not parse, or the values
            are fewer than its highest parameter.
    """
    try:
        tree = parse_sql(sql)
    except Exception as e:
        raise InvalidArgumentError(f"Could not parse the query to put its values in: {e}") from e
    finder = _Params()
    finder(tree)
    if not finder.found:
        if values:
            raise InvalidArgumentError(f"The query has no $1-style parameters for the {len(values)} value(s) given.")
        return sql
    highest = max(number for number, _ in finder.found)
    if highest > len(values):
        raise InvalidArgumentError(f"The query uses ${highest}, and {len(values)} value(s) were given.")
    # pglast's locations count characters of the text it was given; replace
    # from the end so the earlier ones stay where they are.
    text = sql
    for number, location in sorted(finder.found, key=lambda found: -found[1]):
        token = f"${number}"
        if text[location : location + len(token)] != token:
            raise InvalidArgumentError(f"Could not find {token} where the parser placed it.")
        text = text[:location] + literal(values[number - 1]) + text[location + len(token) :]
    return text


def _label(variant: int, values: Sequence[Any] | None, several_sets: bool) -> str:
    letter = chr(ord("A") + variant) if variant < 26 else f"V{variant + 1}"
    if values is None or not several_sets:
        return letter
    return f"{letter} ({', '.join(literal(v) for v in values)})"


def _totals(node: dict[str, Any]) -> tuple[int, int, int]:
    """The top node's buffer counters, which include every node beneath it."""
    return int(node.get("Shared Hit Blocks") or 0), int(node.get("Shared Read Blocks") or 0), int(node.get("Temp Written Blocks") or 0)


async def compare_queries(
    diagnoser: Any,
    queries: Sequence[str],
    value_sets: Sequence[Sequence[Any]] | None = None,
    repeat: int = 3,
    min_ratio: float | None = None,
) -> QueryComparison:
    """Run each query with each set of values, ``repeat`` times, and compare them.

    Args:
        diagnoser: A :class:`~pg_debug.plans.PlanDiagnoser` with a connection.
        queries: The variants: one statement each.
        value_sets: Values for ``$1``, ``$2``..., one set per run; None runs
            each variant as written, its parameters filled from the column
            statistics as ``diagnose`` fills them.
        repeat: How many times to run each combination; the median is
            reported.
        min_ratio: As ``diagnose`` takes it.

    Raises:
        InvalidArgumentError: For no queries, a repeat or a number of runs
            out of range, or values that do not fit a query.
    """
    from .diagnoser import DEFAULT_MIN_RATIO

    diagnoser.ensure_can_run()
    if not queries:
        raise InvalidArgumentError("Give at least one query to run.")
    if not 1 <= repeat <= MAX_REPEAT:
        raise InvalidArgumentError(f"Run each combination between 1 and {MAX_REPEAT} times, not {repeat}.")
    sets: list[Sequence[Any] | None] = list(value_sets) if value_sets else [None]
    if len(queries) * len(sets) > MAX_RUNS:
        raise InvalidArgumentError(
            f"{len(queries)} queries with {len(sets)} sets of values is {len(queries) * len(sets)} runs; the limit is {MAX_RUNS}."
        )
    # Every statement is bound before anything runs, so a value that does not
    # fit fails before the first query has run.
    planned = [(variant, values, bind(query, values) if values is not None else query) for variant, query in enumerate(queries) for values in sets]

    comparison = QueryComparison(runs=[])
    for variant, values, sql in planned:
        run = QueryRun(label=_label(variant, values, len(sets) > 1), variant=variant, values=list(values) if values is not None else None, query=sql)
        comparison.runs.append(run)
        document: dict[str, Any] = {}
        plan: Any = None
        try:
            for _ in range(repeat):
                document, plan = await diagnoser.run(sql)
                run.runs_ms.append(round(plan.execution_time or 0.0, 3))
            run.findings = await diagnoser.findings(plan, min_ratio=min_ratio or DEFAULT_MIN_RATIO)
        except ExplainPlanError as e:
            # One variant that fails to run (a typo, a missing column) is a
            # result; the others still run.
            run.error = str(e)
            continue
        top = document.get("Plan") or {}
        run.execution_ms = round(statistics.median(run.runs_ms), 3)
        run.planning_ms = plan.planning_time
        run.rows = int((top.get("Actual Rows") or 0) * (top.get("Actual Loops") or 1))
        run.shared_hit, run.shared_read, run.temp_written = _totals(top)
        run.shape = shape_lines(document)
        run.fingerprint = shape_fingerprint(run.shape)

    measured = [run for run in comparison.runs if run.execution_ms is not None]
    if measured:
        comparison.fastest = min(measured, key=lambda run: run.execution_ms or 0.0).label
    comparison.notes = _notes(comparison.runs, several_variants=len(queries) > 1, repeat=repeat)
    return comparison


def _notes(runs: list[QueryRun], several_variants: bool, repeat: int) -> list[str]:
    notes: list[str] = []
    measured = [run for run in runs if run.execution_ms is not None]
    if several_variants:
        # Variants run with the same values should return the same rows.
        by_values: dict[str, dict[int, int | None]] = {}
        for run in measured:
            by_values.setdefault(json.dumps(run.values, default=str), {})[run.variant] = run.rows
        for counts in by_values.values():
            if len(set(counts.values())) > 1:
                shown = ", ".join(f"{_label(variant, None, False)} {rows:,}" for variant, rows in counts.items() if rows is not None)
                notes.append(
                    f"The variants returned different numbers of rows ({shown}): they are not the same query, and the faster "
                    "one may be faster because it does less."
                )
                break
    by_variant: dict[int, set[str | None]] = {}
    for run in measured:
        by_variant.setdefault(run.variant, set()).add(run.fingerprint)
    for variant, shapes in by_variant.items():
        if len(shapes) > 1:
            notes.append(
                f"Variant {_label(variant, None, False)} got {len(shapes)} different plans for different values: the planner chose "
                "per value, and a prepared statement that settles on a generic plan after five runs may keep the one that suits "
                "none of them (plan_cache_mode = force_custom_plan keeps choosing)."
            )
    if repeat > 1:
        cold = [
            run.label
            for run in measured
            if len(run.runs_ms) > 1
            and run.runs_ms[0] > 2 * statistics.median(run.runs_ms[1:])
            # A tenth of a millisecond against none is noise, not a cache.
            and run.runs_ms[0] - statistics.median(run.runs_ms[1:]) > 1.0
        ]
        if cold:
            notes.append(
                f"The first run of {', '.join(cold)} took over twice the median of the rest: it read pages the later runs found "
                "in the cache. The median is the warm figure; a query that runs rarely pays the cold one."
            )
    else:
        notes.append(
            "Each combination ran once: the first run may pay for reading pages that later ones find in the cache. --repeat 3 or more evens it out."
        )
    return notes


__all__ = ["MAX_REPEAT", "MAX_RUNS", "QueryComparison", "QueryRun", "bind", "compare_queries", "literal"]
