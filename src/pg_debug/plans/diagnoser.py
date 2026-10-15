"""Running the plan, and gathering what the rules need to read it.

The plan itself answers most of the questions — a sort either spilled or it did
not — but the two most useful findings are about *why* an estimate was wrong,
and neither can be settled from the plan alone. Whether the table's statistics
are current, and whether anything already tells the planner that two of its
columns are related, are facts about the catalog. Those are read here, once for
the whole plan, and handed to the rules as a
:class:`~pg_debug.plans.rules.RuleContext`.

``EXPLAIN ANALYZE`` runs the query. That is the entire reason it can report what
really happened, and it is also why this refuses to work in restricted mode,
whose promise is that nothing executes. For a statement that writes, the run
happens inside a transaction that is rolled back — see
:meth:`~pg_debug.db.SqlDriver.execute_query_and_rollback` for what that does
and does not undo.

A plan that already exists — ``auto_explain`` output, a plan a colleague
pasted, one taken on a replica — can be read instead of produced:
:meth:`PlanDiagnoser.diagnose_plan` and the module-level :func:`diagnose_plan`
take ``EXPLAIN (ANALYZE, FORMAT JSON)`` output. Reading runs nothing, so it is
allowed in restricted mode, and with no connection at all. Without one the
catalog questions go unanswered, and the findings say so rather than passing
the silence off as a clean bill.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any
from typing import LiteralString
from typing import cast

from pglast import ast
from pglast import parse_sql
from pglast.parser import ParseError

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..errors import ExplainPlanError
from ..errors import InvalidArgumentError
from ..parsing import SqlBindParams
from ..statistics import StatisticsInspector
from .metrics import qualified_relation
from .metrics import total_self_ms
from .metrics import walk
from .metrics import walk_with_parent
from .result import DEFAULT_MIN_RATIO
from .result import PlanFinding
from .rules import RULES
from .rules import RuleContext
from .tree import ExplainPlanArtifact
from .tree import PlanNode

logger = logging.getLogger(__name__)

#: Statement types that read and do not write, and so can simply be run.
#: Anything else is run inside a transaction that is rolled back.
_READ_ONLY_STATEMENTS = (ast.SelectStmt,)

COLUMNS_SQL = """
SELECT n.nspname AS schema, c.relname AS table, a.attname AS column
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND (n.nspname || '.' || c.relname) = ANY({}::text[])
"""

EXTENDED_STATISTICS_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    (SELECT array_agg(a.attname ORDER BY a.attnum)
       FROM pg_attribute a
      WHERE a.attrelid = c.oid AND a.attnum = ANY(s.stxkeys)) AS columns
FROM pg_statistic_ext s
JOIN pg_class c ON c.oid = s.stxrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE (n.nspname || '.' || c.relname) = ANY({}::text[])
"""


# Which of the plan's relations are partitions: the table each belongs to, how
# many partitions that table has, and the key columns of every level of
# partitioning above it, any of which a condition could prune on.
PARTITIONS_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    rn.nspname || '.' || r.relname AS root,
    (SELECT count(*) FROM pg_partition_tree(r.oid) AS t WHERE t.isleaf) AS partitions,
    (SELECT array_agg(DISTINCT a.attname)
       FROM pg_partition_ancestors(c.oid) AS anc
       JOIN pg_partitioned_table AS pt ON pt.partrelid = anc.relid
       CROSS JOIN LATERAL unnest(pt.partattrs::int2[]) AS k(attnum)
       JOIN pg_attribute AS a ON a.attrelid = anc.relid AND a.attnum = k.attnum) AS key_columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class r ON r.oid = pg_partition_root(c.oid)
JOIN pg_namespace rn ON rn.oid = r.relnamespace
WHERE c.relispartition
  AND (n.nspname || '.' || c.relname) = ANY({}::text[])
"""


#: What :func:`parse_plan` accepts: the JSON text EXPLAIN printed, or that
#: text already decoded — the list EXPLAIN wraps its one plan in, or the plan
#: object itself, which is also the shape ``auto_explain`` logs.
PlanInput = str | bytes | dict[str, Any] | list[Any]


def parse_plan(plan: PlanInput) -> PlanNode:
    """Read ``EXPLAIN (ANALYZE, FORMAT JSON)`` output into a plan tree.

    Args:
        plan: The output as text, or already decoded. EXPLAIN wraps its plan
            in a one-element list; ``auto_explain`` logs the object inside
            it, alongside ``Query Text``. Both are taken.

    Raises:
        InvalidArgumentError: If it is not JSON, not the shape EXPLAIN
            produces, or was produced without ANALYZE — a diagnosis compares
            the planner's estimates with what the run found, so a plan that
            was never run has nothing to compare.
        ExplainPlanError: If the plan JSON could not be read into a tree.
    """
    data: Any = plan
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise InvalidArgumentError(
                "The plan is not JSON. Produce it with EXPLAIN (ANALYZE, FORMAT JSON), or auto_explain.log_format = 'json'; "
                "the text format EXPLAIN prints by default is not read."
            ) from e
    if isinstance(data, list):
        if len(data) != 1 or not isinstance(data[0], dict):
            raise InvalidArgumentError(f"Expected EXPLAIN's one-element list holding the plan, got a list of {len(data)}.")
        data = data[0]
    if not isinstance(data, dict) or "Plan" not in data:
        raise InvalidArgumentError("Expected EXPLAIN (FORMAT JSON) output: an object with a 'Plan' key, or the one-element list EXPLAIN wraps it in.")

    try:
        root = ExplainPlanArtifact.from_json_data(data).plan_tree
    except Exception as e:
        raise ExplainPlanError(f"Could not read the plan: {e}") from e

    if root.actual_rows is None:
        raise InvalidArgumentError(
            "The plan was never run: it has the planner's estimates but no actual rows, and a diagnosis is the comparison "
            "of the two. Produce it with EXPLAIN (ANALYZE, FORMAT JSON)."
        )
    return root


def offline_context(root: PlanNode, min_ratio: float = DEFAULT_MIN_RATIO) -> RuleContext:
    """A rule context over the plan alone, with the catalog marked unconsulted."""
    return RuleContext(
        parents={id(node): parent for node, parent in walk_with_parent(root)},
        total_ms=total_self_ms(root),
        min_ratio=min_ratio,
        catalog=False,
    )


def run_rules(root: PlanNode, context: RuleContext) -> list[PlanFinding]:
    """Every rule over the tree, most severe finding first."""
    findings: list[PlanFinding] = []
    for rule in RULES:
        findings.extend(rule(root, context))
    return sorted(findings, key=lambda finding: finding.sort_key)


def _check_min_ratio(min_ratio: float) -> None:
    if min_ratio < 1:
        raise InvalidArgumentError(f"min_ratio must be at least 1, since an estimate cannot be less than 1x out. Got {min_ratio}.")


def diagnose_plan(plan: PlanInput, min_ratio: float = DEFAULT_MIN_RATIO) -> list[PlanFinding]:
    """Diagnose a plan with no database behind it.

    Everything the plan itself can answer is answered: the lowest misestimate
    and what it misled, nested loops that blew up, scans discarding rows,
    sorts and hashes and bitmaps that outgrew ``work_mem``, workers not
    granted. The two findings that explain an estimate — stale statistics,
    correlated columns — need the catalog, so a misestimate here says that it
    could not be checked rather than implying it was.

    Args:
        plan: ``EXPLAIN (ANALYZE, FORMAT JSON)`` output; see :func:`parse_plan`.
        min_ratio: How far an estimate has to be from the truth before it is
            reported, as a factor.

    Returns:
        Findings, most severe first. An empty list means the plan is a
        reasonable one, as far as the plan alone can tell.
    """
    _check_min_ratio(min_ratio)
    root = parse_plan(plan)
    return run_rules(root, offline_context(root, min_ratio))


class PlanDiagnoser:
    """Runs a query, reads the plan it produced, and reports what went wrong.

    Given no driver, it can still read plans handed to it; it just cannot run
    anything or ask the catalog anything.
    """

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver | None = None):
        self.sql_driver = sql_driver

    async def diagnose(self, sql: str, min_ratio: float = DEFAULT_MIN_RATIO) -> list[PlanFinding]:
        """Diagnose one statement's execution plan.

        Args:
            sql: The statement to run and read the plan of.
            min_ratio: How far an estimate has to be from the truth before it
                is reported, as a factor.

        Returns:
            Findings, most severe first, each anchored to a node of the plan.
            An empty list means the plan is a reasonable one, which is a real
            answer and the one worth trusting most.
        """
        if self.sql_driver is None:
            raise InvalidArgumentError("Running a statement needs a connection. diagnose_plan() reads a plan that already exists without one.")
        if isinstance(self.sql_driver, SafeSqlDriver):
            raise InvalidArgumentError(
                "Diagnosing a query needs EXPLAIN ANALYZE, which runs it, and restricted mode does not run queries. "
                "Use --access-mode unrestricted against a database where executing this statement is acceptable — a "
                "write is rolled back, but a sequence it consumes is not given back — or hand over a plan you already "
                "have with --plan, which runs nothing."
            )
        _check_min_ratio(min_ratio)

        read_only = self._is_read_only(sql)
        prepared = await self._substitute_bind_variables(sql)

        plan = await self._explain(prepared, read_only=read_only)
        return await self._diagnose_tree(plan.plan_tree, min_ratio)

    async def diagnose_plan(self, plan: PlanInput, min_ratio: float = DEFAULT_MIN_RATIO) -> list[PlanFinding]:
        """Diagnose a plan that already exists, consulting the catalog where there is one.

        With a driver, the tables the plan reads are checked for stale
        statistics and for extended statistics covering their correlated
        columns, exactly as :meth:`diagnose` does after running the query.
        That is pure reading, so it works in restricted mode. Without a
        driver it is :func:`diagnose_plan`.

        Args:
            plan: ``EXPLAIN (ANALYZE, FORMAT JSON)`` output; see :func:`parse_plan`.
            min_ratio: How far an estimate has to be from the truth before it
                is reported, as a factor.

        Returns:
            Findings, most severe first.
        """
        _check_min_ratio(min_ratio)
        return await self._diagnose_tree(parse_plan(plan), min_ratio)

    async def _diagnose_tree(self, root: PlanNode, min_ratio: float) -> list[PlanFinding]:
        context = offline_context(root, min_ratio) if self.sql_driver is None else await self._context(root, min_ratio=min_ratio)
        return run_rules(root, context)

    def _is_read_only(self, sql: str) -> bool:
        """Whether the statement can simply be run, or has to be undone after.

        An unparseable statement is treated as a write. Guessing wrong in that
        direction costs a rolled-back transaction; guessing wrong in the other
        direction costs the user their data.
        """
        try:
            statements = parse_sql(sql)
        except ParseError as e:
            logger.debug("Could not parse the statement, so it will be rolled back after: %s", e)
            return False

        parsed = [s.stmt for s in statements if s.stmt is not None]
        if len(parsed) != 1:
            raise InvalidArgumentError(f"Diagnose one statement at a time. This is {len(parsed)} statements.")
        return isinstance(parsed[0], _READ_ONLY_STATEMENTS)

    @property
    def _driver(self) -> SqlDriver | SafeSqlDriver:
        """The driver, for the paths that cannot be reached without one."""
        if self.sql_driver is None:
            raise InvalidArgumentError("This needs a connection.")
        return self.sql_driver

    async def _substitute_bind_variables(self, sql: str) -> str:
        """Replace ``$1``-style parameters with values the planner can use.

        ``GENERIC_PLAN``, which is how the rest of the tool explains a
        parameterised query, cannot be combined with ANALYZE — a generic plan
        is by definition one that was never run. So the parameters have to
        become real values, chosen from the column statistics.
        """
        if "$" not in sql:
            return sql
        return await SqlBindParams(self._driver).replace_parameters(sql)

    async def _explain(self, sql: str, read_only: bool) -> ExplainPlanArtifact:
        """Run the statement under EXPLAIN and return the parsed plan.

        VERBOSE is asked for because it is the only way the plan says which
        *schema* a relation is in, and without that a name cannot be looked up
        in the catalog when two schemas hold a table of the same name.
        """
        query = cast(LiteralString, f"EXPLAIN (ANALYZE, VERBOSE, BUFFERS, FORMAT JSON) {sql}")

        try:
            if read_only:
                rows = await self._driver.execute_query(query, in_context=True)
            else:
                rows = await self._driver.execute_query_and_rollback(query, in_context=True)
        except Exception as e:
            raise ExplainPlanError(f"Could not run the statement to diagnose it: {e}") from e

        if not rows:
            raise ExplainPlanError("EXPLAIN ANALYZE returned no plan.")

        payload = rows[0].cells.get("QUERY PLAN")
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise ExplainPlanError(f"Expected a plan from EXPLAIN, got {type(payload)}.")

        try:
            return ExplainPlanArtifact.from_json_data(payload[0])
        except Exception as e:
            raise ExplainPlanError(f"Could not read the plan EXPLAIN returned: {e}") from e

    async def _context(self, root: PlanNode, min_ratio: float) -> RuleContext:
        """Gather the catalog facts the estimate rules need, in three queries."""
        relations = sorted({name for name in (qualified_relation(node) for node in walk(root)) if name and "." in name})

        return RuleContext(
            parents={id(node): parent for node, parent in walk_with_parent(root)},
            total_ms=total_self_ms(root),
            min_ratio=min_ratio,
            stale=await self._stale_relations(relations),
            covered=await self._covered_column_sets(relations),
            columns=await self._relation_columns(relations),
            work_mem=await self._work_mem(),
            partitions=await self._partitions(relations),
        )

    async def _stale_relations(self, relations: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Which of these relations the analyze bookkeeping calls out of date."""
        if not relations:
            return {}
        rows = await StatisticsInspector(self._driver).stale_tables()
        stale = {f"{row['schema']}.{row['table']}": row for row in rows}
        return {name: stale[name] for name in relations if name in stale}

    async def _covered_column_sets(self, relations: Sequence[str]) -> dict[str, list[set[str]]]:
        """The column sets already covered by an extended statistics object."""
        covered: dict[str, list[set[str]]] = {}
        for row in await self._rows(EXTENDED_STATISTICS_SQL, relations):
            columns = row.get("columns") or []
            covered.setdefault(f"{row['schema']}.{row['table']}", []).append({str(c) for c in columns})
        return covered

    async def _relation_columns(self, relations: Sequence[str]) -> dict[str, set[str]]:
        """Every relation's real column names.

        This is what tells a column apart from a type name in a condition
        PostgreSQL rendered for display rather than for a parser.
        """
        columns: dict[str, set[str]] = {}
        for row in await self._rows(COLUMNS_SQL, relations):
            columns.setdefault(f"{row['schema']}.{row['table']}", set()).add(str(row["column"]))
        return columns

    async def _partitions(self, relations: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The plan's relations that are partitions, with their table and its partition keys."""
        return {
            f"{row['schema']}.{row['table']}": {
                "root": row["root"],
                "partitions": int(row["partitions"] or 0),
                "key_columns": list(row["key_columns"] or []),
            }
            for row in await self._rows(PARTITIONS_SQL, relations)
        }

    async def _rows(self, query: LiteralString, relations: Sequence[str]) -> list[dict[str, Any]]:
        if not relations:
            return []
        rows = await SafeSqlDriver.execute_param_query(self._driver, query, [list(relations)])
        return [row.cells for row in rows] if rows else []

    async def _work_mem(self) -> str | None:
        """The ``work_mem`` the query ran with, quoted back in any spill finding.

        Read in the query context, which may be what set it.
        """
        try:
            rows = await self._driver.execute_query("SELECT current_setting('work_mem') AS work_mem", in_context=True)
        except Exception as e:
            logger.debug("Could not read work_mem: %s", e)
            return None
        return str(rows[0].cells["work_mem"]) if rows else None


__all__ = [
    "COLUMNS_SQL",
    "EXTENDED_STATISTICS_SQL",
    "PARTITIONS_SQL",
    "PlanDiagnoser",
    "PlanInput",
    "diagnose_plan",
    "offline_context",
    "parse_plan",
    "run_rules",
]
