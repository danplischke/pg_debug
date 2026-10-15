"""Index candidates where HypoPG cannot be had, from the planner's column statistics.

Without HypoPG nothing can ask the planner what an index would cost, so
nothing here is costed. What can still be read is how selective each column a
query filters on is: ``pg_stats`` keeps the share of nulls and the number of
distinct values the last ANALYZE sampled. This is PgHero's heuristic: put the
most selective equality columns first, and stop adding columns once about 50
rows are expected to be left, since an index that already narrows a table to
a page or two gains nothing from another column. A range column can end the
index but nothing can follow it, because a btree uses only the columns before
a range condition to find where to start.

Every candidate is therefore :attr:`Confidence.CANDIDATE`: inferred from
sampled statistics and the query's text, and to be confirmed with the
planner before anyone builds it. Each carries the figures it rests on and how
to confirm it. What was examined and refused is reported with the figure the
refusal rests on.

On Cloud SQL and AlloyDB, Google's ``google_db_advisor`` extension costs
indexes with its own hypothetical indexes. Where it is installed its
recommendations are read too; they are for the workload it has tracked, not
for the queries given.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import LiteralString

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..db import extensions
from ..errors import InvalidArgumentError
from ..index_use.catalog import COLUMNS_FOR_TABLES_SQL
from ..index_use.catalog import INDEXES_FOR_TABLES_SQL
from ..index_use.predicates import Predicate
from ..index_use.predicates import read_predicates
from ..platform import PlatformDetection
from ..severity import SEVERITY_ORDER
from ..severity import Confidence
from ..severity import Severity

logger = logging.getLogger(__name__)

#: Stop adding columns once this many rows are expected to be left: PgHero's
#: figure, about what one or two heap pages hold.
TARGET_ROWS = 50

#: Below this many rows a table is read whole in a handful of pages, and no
#: index is suggested for it. PgHero's figure as well.
FEW_ROWS = 500

#: The widest candidate, as for the costed advisor.
MAX_WIDTH = 3

#: A candidate that leaves more than this share of the table is refused: the
#: planner reads that much of a table faster in one sequential pass than
#: through an index. A judgment, generous on purpose; the planner's own
#: break-even is usually lower, and only it can say where.
MAX_SHARE_LEFT = 0.2

#: Equality-like operators. ``IN`` and ``= ANY`` are taken as one value, as
#: PgHero takes them, which makes a list of many values look more selective
#: than it is.
EQUALITY = frozenset({"=", "IN", "= ANY", "IS TRUE", "IS NULL"})
#: Conditions a btree serves as a range: they can end an index, but nothing
#: after them narrows the scan.
RANGE = frozenset({"<", "<=", ">", ">=", "BETWEEN", "~~", "IS NOT NULL"})

#: What PgHero assumes a range keeps of the rows left, for want of the
#: histogram the planner would read: (rows + rows per value) / 10.
RANGE_DIVISOR = 10.0

#: Sizes above which a candidate is graded higher. Judgments: an index on a
#: table of a hundred thousand rows that narrows a read to a thousandth of it
#: saves a scan worth noticing; on ten thousand rows less so.
HIGH_ROWS, HIGH_SHARE = 100_000, 0.001
MEDIUM_ROWS, MEDIUM_SHARE = 10_000, 0.01

UNCOSTED_NOTE = (
    "These candidates were not costed by the planner. HypoPG cannot be used here, so nothing could ask the planner "
    "whether it would choose them or what they would save: each is inferred from the query's text and the column "
    "statistics the last ANALYZE sampled (pg_stats), following PgHero's heuristic. Confirm one before building it: "
    "create it on a copy of the database, or with CREATE INDEX CONCURRENTLY where a failed build is acceptable, and "
    "EXPLAIN the query to see whether the plan reads it."
)

#: The rows each table holds, from pg_class. A partitioned table holds none
#: of its own; its leaves are summed.
TABLE_ROWS_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    CASE WHEN c.relkind = 'p'
         THEN (SELECT sum(greatest(p.reltuples, 0))::float8
                 FROM pg_partition_tree(c.oid) AS t
                 JOIN pg_class p ON p.oid = t.relid
                WHERE t.isleaf)
         ELSE c.reltuples::float8
    END AS table_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname || '.' || c.relname) = ANY({}::text[])
"""

#: Each column's share of nulls and number of distinct values. A partitioned
#: or inheritance parent has statistics over its whole tree, which are the
#: ones a query on it reads, so those come first.
COLUMN_STATS_SQL = """
SELECT schemaname AS schema, tablename AS table, attname AS column, null_frac, n_distinct, inherited
FROM pg_stats
WHERE (schemaname || '.' || tablename) = ANY({}::text[])
  AND attname = ANY({}::text[])
ORDER BY inherited DESC
"""

#: Google's advisor, on Cloud SQL and AlloyDB. Not in restricted mode's
#: allowlist: pg-debug cannot confirm it writes nothing.
GOOGLE_DB_ADVISOR = "google_db_advisor"
GOOGLE_DB_ADVISOR_SQL: LiteralString = "SELECT * FROM google_db_advisor_recommend_indexes()"

_PLAIN_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_$]*$")


@dataclass
class ColumnStep:
    """One column of a candidate, and the rows expected to be left after it."""

    column: str
    operator: str
    null_frac: float
    n_distinct: float
    rows_after: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "operator": self.operator,
            "null_frac": round(self.null_frac, 4),
            "n_distinct": self.n_distinct,
            "rows_after": round(self.rows_after, 1),
        }


@dataclass
class UncostedCandidate:
    """An index inferred from statistics, not costed by the planner.

    Attributes:
        table: The table, schema-qualified.
        columns: The index's columns, most selective equality columns first.
        definition: The statement that would build it.
        queries: The queries it was inferred from.
        table_rows: The rows pg_class says the table holds.
        expected_rows: The rows expected to be left after every column.
        steps: Each column with its statistics and the rows left after it.
        severity: How much it is likely to matter: by the table's size and
            the share of it the index leaves.
        confidence: Always :attr:`Confidence.CANDIDATE`.
        notes: Restrictions on the table this candidate leaves out, and why.
        verify: How to confirm it.
    """

    table: str
    columns: list[str]
    definition: str
    queries: list[str]
    table_rows: float
    expected_rows: float
    steps: list[ColumnStep]
    severity: Severity
    confidence: Confidence = Confidence.CANDIDATE
    notes: list[str] = field(default_factory=list)
    verify: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "columns": self.columns,
            "definition": self.definition,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "costed": False,
            "table_rows": round(self.table_rows),
            "expected_rows": round(self.expected_rows, 1),
            "steps": [step.to_dict() for step in self.steps],
            "queries": self.queries,
            "notes": self.notes,
            "verify": self.verify,
        }


@dataclass
class UncostedIndexAdvice:
    """What the statistics suggest where HypoPG cannot be had.

    Attributes:
        hypopg: Why HypoPG cannot be had here, and what that means on this
            platform.
        candidates: The inferred indexes, most severe first.
        refused: Each table of each query that got no candidate, with why
            and the figure that rests on.
        google_db_advisor: Google's own recommendations where its extension
            is installed, or why they could not be read; None where it is not.
        platform: What the server was taken for, and why.
    """

    hypopg: str
    candidates: list[UncostedCandidate] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    google_db_advisor: dict[str, Any] | None = None
    platform: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "costed": False,
            "note": UNCOSTED_NOTE,
            "hypopg": self.hypopg,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "refused": self.refused,
        }
        if self.platform:
            result["platform"] = self.platform
        if self.google_db_advisor is not None:
            result["google_db_advisor"] = self.google_db_advisor
        return result


@dataclass
class _ColumnStats:
    null_frac: float
    n_distinct: float


class StatisticsIndexAdvisor:
    """Suggests indexes for queries from pg_stats, where HypoPG cannot cost them."""

    def __init__(self, sql_driver: SqlDriver):
        self.sql_driver = sql_driver

    async def advise(self, queries: list[str], hypopg_message: str, platform: PlatformDetection | None = None) -> UncostedIndexAdvice:
        """Infer index candidates for each query.

        Args:
            queries: The queries to suggest indexes for.
            hypopg_message: Why HypoPG cannot be had, to report alongside.
            platform: What the server was taken for, which the report names.

        Returns:
            The candidates, the refusals and, where installed, Google's
            advisor's recommendations.
        """
        advice = UncostedIndexAdvice(
            hypopg=hypopg_message,
            platform=f"{platform.platform.label} ({platform.signal})" if platform is not None and platform.known else None,
        )

        parsed: list[tuple[str, set[str]]] = []
        for query in queries:
            try:
                tables = read_predicates(query).tables
            except InvalidArgumentError as e:
                advice.refused.append({"query": query, "table": None, "reason": str(e)})
                continue
            parsed.append((query, tables))

        refs = {ref for _, tables in parsed for ref in tables}
        column_rows = await self._rows(COLUMNS_FOR_TABLES_SQL, [sorted(r for r in refs if "." in r), sorted(r for r in refs if "." not in r)])
        resolved = _resolve(refs, column_rows)
        columns_by_ref = {ref: {str(r["column"]) for r in column_rows if f"{r['schema']}.{r['table']}" == name} for ref, name in resolved.items()}

        tables = sorted(set(resolved.values()))
        table_rows = {
            f"{r['schema']}.{r['table']}": float(r["table_rows"] if r["table_rows"] is not None else -1)
            for r in await self._rows(TABLE_ROWS_SQL, [tables])
        }
        all_columns = sorted({column for columns in columns_by_ref.values() for column in columns})
        stats: dict[tuple[str, str], _ColumnStats] = {}
        for r in await self._rows(COLUMN_STATS_SQL, [tables, all_columns]):
            # Inherited statistics first; the first row for a column wins.
            stats.setdefault(
                (f"{r['schema']}.{r['table']}", str(r["column"])),
                _ColumnStats(float(r["null_frac"] or 0.0), float(r["n_distinct"] or 0.0)),
            )
        indexes: dict[str, list[dict[str, Any]]] = {}
        for r in await self._rows(INDEXES_FOR_TABLES_SQL, [tables, []]):
            indexes.setdefault(f"{r['schema']}.{r['table']}", []).append(r)

        by_definition: dict[str, UncostedCandidate] = {}
        for query, refs_of_query in parsed:
            predicates = read_predicates(query, columns_by_ref)
            for ref in sorted(refs_of_query):
                table = resolved.get(ref)
                if table is None:
                    advice.refused.append({"query": query, "table": ref, "reason": "not found in the catalog as a table, view or materialized view"})
                    continue
                outcome = self._candidate(query, table, predicates.on(ref), table_rows.get(table, -1.0), stats, indexes.get(table, []))
                if isinstance(outcome, dict):
                    advice.refused.append(outcome)
                    continue
                known = by_definition.get(outcome.definition)
                if known is None:
                    by_definition[outcome.definition] = outcome
                else:
                    known.queries.append(query)

        advice.candidates = sorted(by_definition.values(), key=lambda c: (SEVERITY_ORDER[c.severity], c.expected_rows / max(c.table_rows, 1.0)))
        advice.google_db_advisor = await self._google_db_advisor()
        return advice

    def _candidate(
        self,
        query: str,
        table: str,
        predicates: list[Predicate],
        total: float,
        stats: dict[tuple[str, str], _ColumnStats],
        existing: list[dict[str, Any]],
    ) -> UncostedCandidate | dict[str, Any]:
        """The candidate for one table of one query, or the refusal with its figure."""

        def refuse(reason: str, **evidence: Any) -> dict[str, Any]:
            return {"query": query, "table": table, "reason": reason, **evidence}

        if total < 0 or (total == 0 and not any((table, p.column) in stats for p in predicates)):
            return refuse("the table has never been analyzed, so there are no statistics to infer from; run ANALYZE on it", table_rows=total)
        if total < FEW_ROWS:
            return refuse(f"{total:,.0f} rows: a sequential scan reads a table this small in a few pages", table_rows=total)

        notes: list[str] = []
        usable: dict[str, tuple[str, _ColumnStats, float]] = {}
        for p in predicates:
            reason = _unusable(p)
            if reason is None and (table, p.column) not in stats:
                reason = "no statistics in pg_stats: the column was never analyzed, or this role may not read it"
            if reason is None and p.operator not in ("IS NULL", "IS NOT NULL") and stats[(table, p.column)].n_distinct == 0:
                reason = "pg_stats does not know its number of distinct values"
            if reason is not None:
                notes.append(f"{p.text or p.column}: {reason}")
                continue
            column_stats = stats[(table, p.column)]
            rows = _rows_after(column_stats, total, total, p.operator)
            if p.column not in usable or rows < usable[p.column][2]:
                usable[p.column] = (p.operator, column_stats, rows)

        if not usable:
            if not predicates:
                return refuse("the query places no restriction on this table that an index could serve", table_rows=total)
            return refuse("none of the query's restrictions on this table can lead a btree index", table_rows=total, skipped=notes)

        steps = _choose(usable, total)
        columns = [step.column for step in steps]
        expected = steps[-1].rows_after

        covered = _covered_by(existing, usable, total, columns, expected)
        if covered is not None:
            name, prefix, rows = covered
            return refuse(
                f"the existing index {name} already serves it: its leading columns ({', '.join(prefix)}) are expected to leave "
                f"{rows:,.0f} rows, no more than the {expected:,.0f} this candidate would",
                table_rows=total,
                existing_index=name,
                expected_rows=round(rows, 1),
            )

        share = expected / total
        if share > MAX_SHARE_LEFT:
            return refuse(
                f"the best index from its statistics, on ({', '.join(columns)}), still leaves {expected:,.0f} of {total:,.0f} rows "
                f"({share:.0%}), more than the {MAX_SHARE_LEFT:.0%} an index is worth reading through",
                table_rows=total,
                expected_rows=round(expected, 1),
            )

        definition = f"CREATE INDEX CONCURRENTLY ON {_quote_table(table)} ({', '.join(_quote(c) for c in columns)})"
        return UncostedCandidate(
            table=table,
            columns=columns,
            definition=definition,
            queries=[query],
            table_rows=total,
            expected_rows=expected,
            steps=steps,
            severity=_grade(total, share),
            notes=notes,
            verify=(
                f"With the index built on a copy, or once HypoPG can be had, EXPLAIN the query and check the plan reads "
                f"an index on {table} ({', '.join(columns)})."
            ),
        )

    async def _google_db_advisor(self) -> dict[str, Any] | None:
        """Google's advisor's recommendations where its extension is installed, or why they could not be read."""
        try:
            status = await extensions.check_extension(self.sql_driver, GOOGLE_DB_ADVISOR, include_messages=False)
        except Exception as e:
            logger.debug("Could not check for %s: %s", GOOGLE_DB_ADVISOR, e)
            return None
        if not status.is_installed:
            return None
        note = (
            "Recommended by Cloud SQL's and AlloyDB's google_db_advisor for the workload it has tracked, not for the queries "
            "given; it costs indexes with hypothetical indexes of its own, which pg-debug did not check."
        )
        try:
            rows = await self.sql_driver.execute_query(GOOGLE_DB_ADVISOR_SQL)
        except Exception as e:
            message = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            return {"recommendations": [], "note": note, "error": f"google_db_advisor_recommend_indexes() could not be run: {message}"}
        return {"recommendations": [dict(row.cells) for row in rows or []], "note": note}

    async def _rows(self, query: LiteralString, params: list[Any]) -> list[dict[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, params)
        return [row.cells for row in rows] if rows else []


def _resolve(refs: set[str], column_rows: list[dict[str, Any]]) -> dict[str, str]:
    """Each table reference as ``schema.table``, from the tables the catalog returned for them."""
    present = {f"{r['schema']}.{r['table']}" for r in column_rows}
    by_name: dict[str, set[str]] = {}
    for name in present:
        by_name.setdefault(name.rpartition(".")[2], set()).add(name)
    resolved: dict[str, str] = {}
    for ref in refs:
        if "." in ref and ref in present:
            resolved[ref] = ref
        elif "." not in ref and len(by_name.get(ref, ())) == 1:
            resolved[ref] = next(iter(by_name[ref]))
    return resolved


def _unusable(p: Predicate) -> str | None:
    """Why this restriction cannot be served by a plain btree column, or None if it can."""
    if p.join_table is not None:
        return (
            f"a join condition with {p.join_table}: whether an index serves it depends on the join the planner picks, which only a costed plan shows"
        )
    if p.or_group is not None:
        return "under an OR, which one index column does not serve alone"
    if p.negated:
        return "negated, which excludes rather than selects"
    if not p.bare:
        return f"the column is inside an expression ({p.expression}), which only an expression index serves"
    if p.operator == "~~":
        if p.pattern is None or p.pattern[:1] in ("%", "_", ""):
            return "a LIKE without a fixed prefix, which a btree cannot use"
        return None
    if p.operator not in EQUALITY and p.operator not in RANGE:
        return f"the operator {p.operator} is not one a btree serves"
    return None


def _rows_after(stats: _ColumnStats, total: float, rows_left: float, operator: str) -> float:
    """PgHero's estimate of the rows left after restricting this column, from what was left before."""
    if operator == "IS NULL":
        return rows_left * stats.null_frac
    if operator == "IS NOT NULL":
        return rows_left * (1 - stats.null_frac)
    rows_left *= 1 - stats.null_frac
    if stats.n_distinct < 0:
        # A negative n_distinct is a share of the rows: -1 means every row
        # differs. Each value then holds -1/n_distinct rows of the table.
        per_value = (-1 / stats.n_distinct) * (rows_left / total) if total > 0 else 0.0
    else:
        per_value = rows_left / stats.n_distinct
    if operator in RANGE:
        return (rows_left + per_value) / RANGE_DIVISOR
    return per_value


def _choose(usable: dict[str, tuple[str, _ColumnStats, float]], total: float) -> list[ColumnStep]:
    """Equality columns, most selective first, then at most one range column, until about TARGET_ROWS are left."""
    equality = sorted((c for c, (op, _, _) in usable.items() if op not in RANGE), key=lambda c: (usable[c][2], c))
    ranges = sorted((c for c, (op, _, _) in usable.items() if op in RANGE), key=lambda c: (usable[c][2], c))

    steps: list[ColumnStep] = []
    rows = total
    for column in equality:
        if (steps and rows <= TARGET_ROWS) or len(steps) >= MAX_WIDTH:
            break
        operator, stats, _ = usable[column]
        rows = _rows_after(stats, total, rows, operator)
        steps.append(ColumnStep(column, operator, stats.null_frac, stats.n_distinct, rows))
    if ranges and len(steps) < MAX_WIDTH and (not steps or rows > TARGET_ROWS):
        column = ranges[0]
        operator, stats, _ = usable[column]
        rows = _rows_after(stats, total, rows, operator)
        steps.append(ColumnStep(column, operator, stats.null_frac, stats.n_distinct, rows))
    return steps


def _covered_by(
    existing: list[dict[str, Any]],
    usable: dict[str, tuple[str, _ColumnStats, float]],
    total: float,
    columns: list[str],
    expected: float,
) -> tuple[str, list[str], float] | None:
    """An existing index whose leading columns already narrow the rows as far, with its matched prefix and rows."""
    for row in existing:
        if row.get("method") != "btree" or not row.get("valid") or not row.get("ready") or row.get("predicate"):
            continue
        keys = [str(c) for c in (row.get("columns") or [])][: int(row.get("key_columns") or 0)]
        prefix: list[str] = []
        rows = total
        for key in keys:
            if key not in usable:
                break
            operator, stats, _ = usable[key]
            rows = _rows_after(stats, total, rows, operator)
            prefix.append(key)
            if operator in RANGE:
                break
        if prefix and (rows <= max(expected, TARGET_ROWS) or columns == keys[: len(columns)]):
            return str(row["index"]), prefix, rows
    return None


def _grade(total: float, share: float) -> Severity:
    if total >= HIGH_ROWS and share <= HIGH_SHARE:
        return Severity.HIGH
    if total >= MEDIUM_ROWS and share <= MEDIUM_SHARE:
        return Severity.MEDIUM
    return Severity.LOW


def _quote(name: str) -> str:
    return name if _PLAIN_IDENTIFIER.match(name) else '"' + name.replace('"', '""') + '"'


def _quote_table(table: str) -> str:
    schema, _, name = table.rpartition(".")
    return f"{_quote(schema)}.{_quote(name)}" if schema else _quote(name)


__all__ = [
    "UNCOSTED_NOTE",
    "ColumnStep",
    "StatisticsIndexAdvisor",
    "UncostedCandidate",
    "UncostedIndexAdvice",
]
