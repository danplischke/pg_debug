"""Planner statistics: what the optimizer believes about your data.

When an execution plan's row estimates are far from reality, the fix is rarely
an index — it is usually stale statistics, or a correlation between columns
that only extended statistics can express. These read what the planner is
actually working from.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict
from typing import List

from .db import SafeSqlDriver
from .db import SqlDriver
from .db import split_table_reference

logger = logging.getLogger(__name__)

#: A table is called stale once this fraction of its rows changed since the
#: last ANALYZE. The planner's estimates drift well before this, but below it
#: the drift is rarely what makes a plan go wrong.
DEFAULT_STALE_FRACTION = 0.2

#: Ignore churn on tiny tables, where a large fraction is a handful of rows.
DEFAULT_MIN_CHANGED_ROWS = 1000


class StatisticsInspector:
    """Reads pg_stats, pg_statistic_ext and the analyze bookkeeping."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def column_stats(self, table: str) -> List[Dict[str, Any]]:
        """Report the planner's per-column statistics for a table.

        Args:
            table: Table name, optionally schema-qualified. Unqualified names
                resolve through the connection's search_path.

        Returns:
            One dict per column: ``null_frac`` (fraction of NULLs),
            ``n_distinct`` (distinct values — negative means a multiple of the
            row count), ``correlation`` (how closely physical order follows the
            column, which decides whether an index scan is cheap), ``avg_width``
            and how many most-common values the planner tracks.

            An empty list means the table has no statistics at all — it has
            never been analyzed, or does not exist.
        """
        schema, name = split_table_reference(table)

        query = """
            SELECT
                s.schemaname AS schema,
                s.tablename AS table,
                s.attname AS column,
                s.inherited,
                s.null_frac,
                s.avg_width,
                s.n_distinct,
                s.correlation,
                cardinality(s.most_common_vals) AS most_common_values
            FROM pg_stats s
            WHERE s.tablename = {}
        """
        params: List[Any] = [name]
        if schema:
            query += " AND s.schemaname = {}"
            params.append(schema)
        else:
            # Without a qualifier, resolve the way the query would: the first
            # schema on the search_path that has this table.
            query += " AND s.schemaname = ANY(current_schemas(true))"
        query += " ORDER BY s.attname"

        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, params)
        return [row.cells for row in rows] if rows else []

    async def extended_statistics(self, table: str) -> List[Dict[str, Any]]:
        """List the extended statistics objects defined on a table.

        Extended statistics are how you tell the planner that columns are
        correlated — without them it multiplies per-column selectivities and
        underestimates rows for multi-column predicates.

        Returns:
            One dict per statistics object, with the columns it covers and the
            kinds it collects (``d`` n-distinct, ``f`` dependencies, ``m`` MCV).
        """
        schema, name = split_table_reference(table)

        query = """
            SELECT
                n.nspname AS schema,
                c.relname AS table,
                s.stxname AS name,
                (SELECT array_agg(a.attname ORDER BY a.attnum)
                   FROM pg_attribute a
                  WHERE a.attrelid = c.oid AND a.attnum = ANY(s.stxkeys)) AS columns,
                s.stxkind AS kinds
            FROM pg_statistic_ext s
            JOIN pg_class c ON c.oid = s.stxrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = {}
        """
        params: List[Any] = [name]
        if schema:
            query += " AND n.nspname = {}"
            params.append(schema)
        else:
            query += " AND n.nspname = ANY(current_schemas(true))"
        query += " ORDER BY s.stxname"

        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, params)
        return [row.cells for row in rows] if rows else []

    async def stale_tables(
        self,
        stale_fraction: float = DEFAULT_STALE_FRACTION,
        min_changed_rows: int = DEFAULT_MIN_CHANGED_ROWS,
    ) -> List[Dict[str, Any]]:
        """Find tables whose statistics no longer describe their contents.

        Args:
            stale_fraction: Report a table once this fraction of its rows has
                been inserted, updated or deleted since the last ANALYZE.
            min_changed_rows: Ignore tables with fewer changed rows than this,
                so small tables do not dominate the report.

        Returns:
            One dict per table, with when it was last analyzed (``null`` if
            never), how many rows changed since, and that as a fraction.
        """
        query = """
            SELECT
                schemaname AS schema,
                relname AS table,
                n_live_tup AS live_rows,
                n_mod_since_analyze AS changed_rows,
                CASE
                    WHEN n_live_tup > 0
                    THEN (n_mod_since_analyze::float8 / n_live_tup)
                END AS changed_fraction,
                GREATEST(last_analyze, last_autoanalyze) AS last_analyzed,
                last_analyze IS NULL AND last_autoanalyze IS NULL AS never_analyzed
            FROM pg_stat_user_tables
            WHERE (last_analyze IS NULL AND last_autoanalyze IS NULL AND n_live_tup > 0)
               OR (n_mod_since_analyze >= {} AND n_live_tup > 0 AND n_mod_since_analyze::float8 / n_live_tup >= {})
            ORDER BY n_mod_since_analyze DESC
        """
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, [min_changed_rows, stale_fraction])
        return [row.cells for row in rows] if rows else []


__all__ = ["DEFAULT_MIN_CHANGED_ROWS", "DEFAULT_STALE_FRACTION", "StatisticsInspector"]
