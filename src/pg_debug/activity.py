"""What the server is doing right now: backends, locks and vacuum progress.

These read the live activity views rather than cumulative statistics, so they
answer "what is happening" rather than "what has happened".
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from .db import SafeSqlDriver
from .db import SqlDriver
from .db import get_postgres_version

logger = logging.getLogger(__name__)

# pg_stat_activity.query_id was added in PostgreSQL 14.
QUERY_ID_MIN_VERSION = 14

# pg_stat_progress_vacuum replaced its dead-tuple counters in PostgreSQL 17:
# max_dead_tuples/num_dead_tuples became num_dead_item_ids plus byte-based
# counters, and index progress was added.
VACUUM_PROGRESS_NEW_COUNTERS_MIN_VERSION = 17

_LEGACY_VACUUM_FIELDS = """
    v.num_dead_tuples AS dead_item_ids,
    v.max_dead_tuples AS max_dead_item_ids,
    NULL::bigint AS dead_tuple_bytes,
    NULL::bigint AS max_dead_tuple_bytes,
    NULL::bigint AS indexes_total,
    NULL::bigint AS indexes_processed
"""

_MODERN_VACUUM_FIELDS = """
    v.num_dead_item_ids AS dead_item_ids,
    NULL::bigint AS max_dead_item_ids,
    v.dead_tuple_bytes,
    v.max_dead_tuple_bytes,
    v.indexes_total,
    v.indexes_processed
"""


class ActivityInspector:
    """Reads pg_stat_activity and the vacuum progress view."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def list_backends(
        self,
        include_idle: bool = False,
        min_duration_seconds: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """List server processes and what they are doing.

        Args:
            include_idle: Include idle client backends and idle auxiliary
                processes. They are hidden by default because on a healthy
                server they are most of the list and none of the signal.
            min_duration_seconds: Only report backends that have been in their
                current state at least this long — the way to ask for queries
                running over 30s, or transactions left idle for minutes.

        Returns:
            One dict per backend. ``blocked_by`` holds the pids blocking it
            (empty when it is not blocked), and the ``*_seconds`` fields give
            the age of the current state, query and transaction.

        Note:
            Without the pg_read_all_stats role or superuser, other users'
            query text reads ``<insufficient privilege>``. That is PostgreSQL
            redacting it, not a failure here.
        """
        query_id_field = "a.query_id" if await self._has_query_id() else "NULL::bigint AS query_id"

        # Auxiliary processes (checkpointer, walwriter, ...) sit in a
        # 'Activity' wait when they have nothing to do; a NULL state with any
        # other wait event means they are working.
        idle_filter = (
            ""
            if include_idle
            else """
              AND a.state IS DISTINCT FROM 'idle'
              AND NOT (a.backend_type IS DISTINCT FROM 'client backend' AND a.wait_event_type = 'Activity')
            """
        )

        duration_filter = ""
        params: List[Any] = []
        if min_duration_seconds is not None:
            duration_filter = " AND clock_timestamp() - a.state_change >= make_interval(secs => {})"
            params.append(min_duration_seconds)

        query = f"""
            SELECT
                a.pid,
                a.datname,
                a.usename,
                a.application_name,
                a.client_addr::text AS client_addr,
                a.backend_type,
                a.state,
                a.wait_event_type,
                a.wait_event,
                pg_blocking_pids(a.pid) AS blocked_by,
                EXTRACT(EPOCH FROM clock_timestamp() - a.state_change)::float8 AS state_seconds,
                EXTRACT(EPOCH FROM clock_timestamp() - a.query_start)::float8 AS query_seconds,
                EXTRACT(EPOCH FROM clock_timestamp() - a.xact_start)::float8 AS transaction_seconds,
                {query_id_field},
                a.query
            FROM pg_stat_activity a
            WHERE a.pid <> pg_backend_pid()
            {idle_filter}
            {duration_filter}
            ORDER BY a.state_change ASC NULLS LAST
            """

        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, params)
        return [row.cells for row in rows] if rows else []

    async def blocked_backends(self) -> List[Dict[str, Any]]:
        """List only the backends waiting on a lock held by another backend."""
        return [row for row in await self.list_backends(include_idle=True) if row.get("blocked_by")]

    async def vacuum_progress(self) -> List[Dict[str, Any]]:
        """Report vacuums currently running, and how far along they are.

        Returns:
            One dict per running vacuum, including ``percent_scanned`` and the
            phase. Counter fields differ by server version: PostgreSQL 17
            reports dead tuples in bytes and tracks index progress, older
            versions report a dead item id count and its ceiling. Fields the
            server does not provide come back as None.
        """
        pg_version = await get_postgres_version(self.sql_driver)
        counters = _MODERN_VACUUM_FIELDS if pg_version >= VACUUM_PROGRESS_NEW_COUNTERS_MIN_VERSION else _LEGACY_VACUUM_FIELDS

        query = f"""
            SELECT
                v.pid,
                v.datname,
                n.nspname AS schema,
                c.relname AS table,
                v.phase,
                v.heap_blks_total,
                v.heap_blks_scanned,
                v.heap_blks_vacuumed,
                v.index_vacuum_count,
                {counters},
                CASE
                    WHEN v.heap_blks_total > 0
                    THEN round(100.0 * v.heap_blks_scanned / v.heap_blks_total, 1)::float8
                END AS percent_scanned,
                a.usename,
                a.query LIKE 'autovacuum:%' AS autovacuum,
                EXTRACT(EPOCH FROM clock_timestamp() - a.query_start)::float8 AS running_seconds,
                a.query
            FROM pg_stat_progress_vacuum v
            LEFT JOIN pg_stat_activity a ON a.pid = v.pid
            LEFT JOIN pg_class c ON c.oid = v.relid
            LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
            ORDER BY v.pid
            """

        rows = await self.sql_driver.execute_query(query)
        return [row.cells for row in rows] if rows else []

    async def _has_query_id(self) -> bool:
        return await get_postgres_version(self.sql_driver) >= QUERY_ID_MIN_VERSION


__all__ = ["ActivityInspector"]
