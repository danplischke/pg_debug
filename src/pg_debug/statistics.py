"""Planner statistics: what the optimizer believes about your data.

When an execution plan's row estimates are far from reality, the fix is rarely
an index — it is usually stale statistics, or a correlation between columns
that only extended statistics can express. These read what the planner is
actually working from.
"""

from __future__ import annotations

import logging
from typing import Any

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

    async def column_stats(self, table: str) -> list[dict[str, Any]]:
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
        params: list[Any] = [name]
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

    async def extended_statistics(self, table: str) -> list[dict[str, Any]]:
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
        params: list[Any] = [name]
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
    ) -> list[dict[str, Any]]:
        """Find tables whose statistics no longer describe their contents.

        Args:
            stale_fraction: Report a table once this fraction of its rows has
                been inserted, updated or deleted since the last ANALYZE.
            min_changed_rows: Ignore tables with fewer changed rows than this,
                so small tables do not dominate the report.

        Returns:
            One dict per table, with when it was last analyzed (``null`` if
            not since statistics were last reset), how many rows changed
            since, and that as a fraction. ``never_analyzed`` is a table with
            data and no statistics at all.

        Whether a table has statistics is read from ``pg_stats``, not from
        the activity counters: a crash or ``pg_stat_reset()`` zeroes those,
        and the times of the last ANALYZE with them, but not the statistics.
        Judged by the counters, a reset made every table look current, and
        one that had never been analyzed was not reported, for want of a row
        count. A table the role may not read is not judged on that, since
        ``pg_stats`` hides its rows from the role.
        """
        query = """
            SELECT
                s.schemaname AS schema,
                s.relname AS table,
                GREATEST(s.n_live_tup, c.reltuples::bigint, 0) AS live_rows,
                pg_relation_size(s.relid) AS size_bytes,
                s.n_mod_since_analyze AS changed_rows,
                CASE
                    WHEN s.n_live_tup > 0
                    THEN (s.n_mod_since_analyze::float8 / s.n_live_tup)
                END AS changed_fraction,
                GREATEST(s.last_analyze, s.last_autoanalyze) AS last_analyzed,
                NOT st.has_statistics AS never_analyzed
            FROM pg_stat_user_tables s
            JOIN pg_class c ON c.oid = s.relid
            CROSS JOIN LATERAL (
                SELECT
                    s.last_analyze IS NOT NULL
                    OR s.last_autoanalyze IS NOT NULL
                    OR EXISTS (SELECT 1 FROM pg_stats p WHERE p.schemaname = s.schemaname AND p.tablename = s.relname)
                    AS has_statistics
            ) st
            WHERE (NOT st.has_statistics AND has_table_privilege(s.relid, 'SELECT') AND pg_relation_size(s.relid) > 0)
               OR (s.n_mod_since_analyze >= {} AND s.n_live_tup > 0 AND s.n_mod_since_analyze::float8 / s.n_live_tup >= {})
            ORDER BY s.n_mod_since_analyze DESC
        """
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, [min_changed_rows, stale_fraction])
        return [row.cells for row in rows] if rows else []


__all__ = ["DEFAULT_MIN_CHANGED_ROWS", "DEFAULT_STALE_FRACTION", "StatisticsInspector"]
