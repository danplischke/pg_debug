"""Running the plan, and gathering what the rules need to read it.

The plan itself answers most of the questions — a sort either spilled or it did
not — but the two most useful findings are about *why* an estimate was wrong,
and neither can be settled from the plan alone. Whether the table's statistics
are current, and whether anything already tells the planner that two of its
columns are related, are facts about the catalog. Those are read here, once for
the whole plan, and handed to the rules as a
:class:`~pg_debug.plan.rules.RuleContext`.

``EXPLAIN ANALYZE`` runs the query. That is the entire reason it can report what
really happened, and it is also why this refuses to work in restricted mode,
whose promise is that nothing executes. For a statement that writes, the run
happens inside a transaction that is rolled back — see
:meth:`~pg_debug.db.SqlDriver.execute_query_and_rollback` for what that does
and does not undo.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict
from typing import List
from typing import LiteralString
from typing import Optional
from typing import Sequence
from typing import Set
from typing import cast

from pglast import ast
from pglast import parse_sql
from pglast.parser import ParseError

from ..artifacts import ExplainPlanArtifact
from ..artifacts import PlanNode
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


class PlanDiagnoser:
    """Runs a query, reads the plan it produced, and reports what went wrong."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def diagnose(self, sql: str, min_ratio: float = DEFAULT_MIN_RATIO) -> List[PlanFinding]:
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
        if isinstance(self.sql_driver, SafeSqlDriver):
            raise InvalidArgumentError(
                "Diagnosing a plan needs EXPLAIN ANALYZE, which runs the query, and restricted mode does not run "
                "queries. Use --access-mode unrestricted against a database where executing this statement is "
                "acceptable — a write is rolled back, but a sequence it consumes is not given back."
            )
        if min_ratio < 1:
            raise InvalidArgumentError(f"min_ratio must be at least 1, since an estimate cannot be less than 1x out. Got {min_ratio}.")

        read_only = self._is_read_only(sql)
        prepared = await self._substitute_bind_variables(sql)

        plan = await self._explain(prepared, read_only=read_only)
        context = await self._context(plan.plan_tree, min_ratio=min_ratio)

        findings: List[PlanFinding] = []
        for rule in RULES:
            findings.extend(rule(plan.plan_tree, context))
        return sorted(findings, key=lambda finding: finding.sort_key)

    def _is_read_only(self, sql: str) -> bool:
        """Whether the statement can simply be run, or has to be undone after.

        An unparseable statement is treated as a write. Guessing wrong in that
        direction costs a rolled-back transaction; guessing wrong in the other
        direction costs the user their data.
        """
        try:
            statements = parse_sql(sql)
        except ParseError as e:
            logger.debug(f"Could not parse the statement, so it will be rolled back after: {e}")
            return False

        parsed = [s.stmt for s in statements if s.stmt is not None]
        if len(parsed) != 1:
            raise InvalidArgumentError(f"Diagnose one statement at a time. This is {len(parsed)} statements.")
        return isinstance(parsed[0], _READ_ONLY_STATEMENTS)

    async def _substitute_bind_variables(self, sql: str) -> str:
        """Replace ``$1``-style parameters with values the planner can use.

        ``GENERIC_PLAN``, which is how the rest of the tool explains a
        parameterised query, cannot be combined with ANALYZE — a generic plan
        is by definition one that was never run. So the parameters have to
        become real values, chosen from the column statistics.
        """
        if "$" not in sql:
            return sql
        return await SqlBindParams(self.sql_driver).replace_parameters(sql)

    async def _explain(self, sql: str, read_only: bool) -> ExplainPlanArtifact:
        """Run the statement under EXPLAIN and return the parsed plan.

        VERBOSE is asked for because it is the only way the plan says which
        *schema* a relation is in, and without that a name cannot be looked up
        in the catalog when two schemas hold a table of the same name.
        """
        query = cast(LiteralString, f"EXPLAIN (ANALYZE, VERBOSE, BUFFERS, FORMAT JSON) {sql}")

        try:
            if read_only:
                rows = await self.sql_driver.execute_query(query)
            else:
                rows = await self.sql_driver.execute_query_and_rollback(query)
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
        )

    async def _stale_relations(self, relations: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Which of these relations the analyze bookkeeping calls out of date."""
        if not relations:
            return {}
        rows = await StatisticsInspector(self.sql_driver).stale_tables()
        stale = {f"{row['schema']}.{row['table']}": row for row in rows}
        return {name: stale[name] for name in relations if name in stale}

    async def _covered_column_sets(self, relations: Sequence[str]) -> Dict[str, List[Set[str]]]:
        """The column sets already covered by an extended statistics object."""
        covered: Dict[str, List[Set[str]]] = {}
        for row in await self._rows(EXTENDED_STATISTICS_SQL, relations):
            columns = row.get("columns") or []
            covered.setdefault(f"{row['schema']}.{row['table']}", []).append({str(c) for c in columns})
        return covered

    async def _relation_columns(self, relations: Sequence[str]) -> Dict[str, Set[str]]:
        """Every relation's real column names.

        This is what tells a column apart from a type name in a condition
        PostgreSQL rendered for display rather than for a parser.
        """
        columns: Dict[str, Set[str]] = {}
        for row in await self._rows(COLUMNS_SQL, relations):
            columns.setdefault(f"{row['schema']}.{row['table']}", set()).add(str(row["column"]))
        return columns

    async def _rows(self, query: LiteralString, relations: Sequence[str]) -> List[Dict[str, Any]]:
        if not relations:
            return []
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, [list(relations)])
        return [row.cells for row in rows] if rows else []

    async def _work_mem(self) -> Optional[str]:
        """The session's ``work_mem``, quoted back in any spill finding."""
        try:
            rows = await self.sql_driver.execute_query("SELECT current_setting('work_mem') AS work_mem")
        except Exception as e:
            logger.debug(f"Could not read work_mem: {e}")
            return None
        return str(rows[0].cells["work_mem"]) if rows else None


__all__ = ["COLUMNS_SQL", "EXTENDED_STATISTICS_SQL", "PlanDiagnoser"]
