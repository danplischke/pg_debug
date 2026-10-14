"""What the server is doing right now: backends, locks and vacuum progress.

These read the live activity views rather than cumulative statistics, so they
answer "what is happening" rather than "what has happened".

They return what PostgreSQL shows the connecting role. Without
``pg_read_all_stats``, which ``pg_monitor`` includes, that is other roles'
backends, and the server's own processes, with their state, query, wait and
timings withheld; :mod:`pg_debug.db.privileges` says what was withheld and
why.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any
from typing import LiteralString
from typing import cast

from .db import SafeSqlDriver
from .db import SqlDriver
from .db import get_postgres_version
from .db.privileges import HIDDEN_QUERY_TEXT
from .db.privileges import IN_TRANSACTION
from .db.privileges import IN_TRANSACTION_CTE

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

#: Which backends ``list_backends`` leaves out unless asked for idle ones.
#:
#: Auxiliary processes (checkpointer, walwriter, ...) sit in an 'Activity'
#: wait when they have nothing to do; a NULL state with any other wait event
#: means they are working. A backend whose details are withheld from the role
#: has NULL for all of these, so it is judged by what is not withheld: one in
#: a transaction is listed, as an idle one in a transaction would be, and one
#: that shows no sign of a transaction is left out with the idle ones.
NOT_IDLE = f"""
    AND CASE
        WHEN a.query = '{HIDDEN_QUERY_TEXT}' THEN {IN_TRANSACTION}
        ELSE a.state IS DISTINCT FROM 'idle'
         AND NOT (a.backend_type IS DISTINCT FROM 'client backend' AND a.wait_event_type = 'Activity')
    END
"""


class ActivityInspector:
    """Reads pg_stat_activity and the vacuum progress view."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def list_backends(
        self,
        include_idle: bool = False,
        min_duration_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
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
            (empty when it is not blocked), the ``*_seconds`` fields give the
            age of the current state, query and transaction, and
            ``in_transaction`` says whether it is in one.

        Note:
            Without pg_read_all_stats, which pg_monitor includes, PostgreSQL
            shows other roles' backends, and its own processes, with no state,
            wait or timings, and ``<insufficient privilege>`` for the query.
            Whether such a backend is in a transaction can still be read, from
            its transaction ID, its snapshot and its locks, so one that is in
            a transaction is listed with the running ones and the rest are
            left out with the idle ones. How long it has been in its state
            cannot be read, so with ``min_duration_seconds`` it is not listed.
            :func:`pg_debug.db.privileges.withheld_backends` says what is
            known of each.
        """
        query_id_field = "a.query_id" if await self._has_query_id() else "NULL::bigint AS query_id"
        idle_filter = "" if include_idle else NOT_IDLE

        duration_filter = ""
        params: list[Any] = []
        if min_duration_seconds is not None:
            duration_filter = " AND clock_timestamp() - a.state_change >= make_interval(secs => {})"
            params.append(min_duration_seconds)

        query = f"""
            WITH {IN_TRANSACTION_CTE}
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
                {IN_TRANSACTION} AS in_transaction,
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

    async def blocked_backends(self) -> list[dict[str, Any]]:
        """List only the backends waiting on a lock held by another backend.

        A waiter whose details PostgreSQL withholds from the role is listed
        too: who blocks whom is read from the locks, which every role is
        shown. The backends it waits on are in its ``blocked_by`` whether
        their details are withheld or not.
        """
        return [row for row in await self.list_backends(include_idle=True) if row.get("blocked_by")]

    async def vacuum_progress(self) -> list[dict[str, Any]]:
        """Report vacuums currently running, and how far along they are.

        Returns:
            One dict per running vacuum, including ``percent_scanned`` and the
            phase. Counter fields differ by server version: PostgreSQL 17
            reports dead tuples in bytes and tracks index progress, older
            versions report a dead item id count and its ceiling. Fields the
            server does not provide come back as None.

        Note:
            Without pg_read_all_stats, which pg_monitor includes, the vacuums
            of other roles and of autovacuum come back with no table, phase or
            counters, and ``<insufficient privilege>`` for the query. Whether
            one is autovacuum is then None, not False: it is read from the
            backend's type, which is withheld with the rest.
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
                a.backend_type = 'autovacuum worker' AS autovacuum,
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

    async def lock_tree(self) -> list[dict[str, Any]]:
        """Who is blocking whom, as trees rooted at the backends that wait on nothing.

        ``list_backends`` says which pids block each waiter. When the database
        is stuck the question is the other way round: which one session is at
        the root, what is it doing, and what does everything else wait for.
        This walks the blocking graph from every waiter back to a backend that
        is itself not waiting, and reports each such root with the backends
        behind it nested under it.

        Returns:
            One dict per root, the root blocking the most backends first. Each
            node carries the backend's identity, state, how long it has been in
            it, whether it is in a transaction, its query, ``blocked_by``,
            ``blocking`` (the nodes waiting on it, nested the same way) and
            ``blocked_total`` (distinct backends anywhere beneath it). A
            non-root node also carries ``waits_for``: the lock type and mode it
            is waiting on, the relation when the catalog of this database can
            name it (another database's shows as an OID), the transaction id
            for a row-lock wait with the table the row is in, the modes its
            parent holds on that target, and a one-line ``description`` of all
            that.

            A backend waiting on several holders appears under each of them.
            Two backends waiting on each other (a deadlock, which the server
            resolves within ``deadlock_timeout``) are reported once, under a
            node marked ``cycle``.

        Note:
            This reads pg_stat_activity and pg_locks in two statements, so a
            lock released between them shows as a wait on "a lock pg_locks no
            longer shows". Run it again; a pile-up that matters is still there.

            Without pg_read_all_stats, which pg_monitor includes, other roles'
            backends come back with no state, wait or timings, and
            ``<insufficient privilege>`` for the query. The trees are still
            whole, and a backend whose details are withheld is still in them,
            since every role is shown every lock.
        """
        rows = await self.sql_driver.execute_query(LOCK_TREE_BACKENDS_SQL)
        backends = [row.cells for row in rows] if rows else []
        if not backends:
            return []
        rows = await self.sql_driver.execute_query(cast(LiteralString, LOCK_TREE_HOLDERS_SQL))
        holders = [row.cells for row in rows] if rows else []
        return assemble_lock_tree(backends, holders)


# -- Lock wait tree ---------------------------------------------------------

#: The backends in a lock pile-up: every one waiting on a lock, and every one a
#: waiter points at. A backend waits on at most one lock, so the LATERAL picks
#: it up directly from pg_locks. The relation is named where this database's
#: catalog can name it. A wait on another transaction is how a row lock waits,
#: and the waiter's own granted tuple lock says which table the row is in.
#: Whether each is in a transaction is read as :data:`IN_TRANSACTION` reads
#: it, which also answers for a backend whose state is withheld.
LOCK_TREE_BACKENDS_SQL = f"""
    WITH involved AS (
        SELECT a.pid, pg_blocking_pids(a.pid) AS blocked_by
        FROM pg_stat_activity a
        WHERE a.pid <> pg_backend_pid()
    ),
    {IN_TRANSACTION_CTE}
    SELECT
        a.pid,
        a.datname,
        a.usename,
        a.application_name,
        a.backend_type,
        a.state,
        a.wait_event_type,
        a.wait_event,
        i.blocked_by,
        EXTRACT(EPOCH FROM clock_timestamp() - a.state_change)::float8 AS state_seconds,
        EXTRACT(EPOCH FROM clock_timestamp() - a.query_start)::float8 AS query_seconds,
        EXTRACT(EPOCH FROM clock_timestamp() - a.xact_start)::float8 AS transaction_seconds,
        {IN_TRANSACTION} AS in_transaction,
        a.query,
        w.locktype AS waiting_locktype,
        w.mode AS waiting_mode,
        w.relation AS waiting_relation_oid,
        CASE WHEN wc.oid IS NOT NULL THEN quote_ident(wn.nspname) || '.' || quote_ident(wc.relname) END AS waiting_relation,
        w.transactionid::text AS waiting_transactionid,
        CASE WHEN tc.oid IS NOT NULL THEN quote_ident(tn.nspname) || '.' || quote_ident(tc.relname) END AS tuple_relation
    FROM pg_stat_activity a
    JOIN involved i ON i.pid = a.pid
    LEFT JOIN LATERAL (
        SELECT l.locktype, l.mode, l.relation, l.transactionid
        FROM pg_locks l
        WHERE l.pid = a.pid AND NOT l.granted
        LIMIT 1
    ) w ON true
    LEFT JOIN pg_class wc ON wc.oid = w.relation
    LEFT JOIN pg_namespace wn ON wn.oid = wc.relnamespace
    LEFT JOIN LATERAL (
        SELECT l.relation
        FROM pg_locks l
        WHERE l.pid = a.pid AND l.granted AND l.locktype = 'tuple'
        LIMIT 1
    ) t ON true
    LEFT JOIN pg_class tc ON tc.oid = t.relation
    LEFT JOIN pg_namespace tn ON tn.oid = tc.relnamespace
    WHERE cardinality(i.blocked_by) > 0
       OR a.pid IN (SELECT unnest(blocked_by) FROM involved)
    ORDER BY a.pid
"""

#: For every lock being waited on, who holds a lock on the same target and in
#: which mode. A lock's target is the whole set of identifying columns, most of
#: them NULL for any given lock type, which is what IS NOT DISTINCT FROM is for.
#: Not every holder on a target blocks the waiter; pg_blocking_pids decides
#: that, and this only supplies the mode for the pairs it names.
LOCK_TREE_HOLDERS_SQL = """
    SELECT w.pid AS waiter, h.pid AS holder, h.mode AS held_mode
    FROM pg_locks w
    JOIN pg_locks h
      ON h.granted
     AND h.pid <> w.pid
     AND h.locktype = w.locktype
     AND h.database IS NOT DISTINCT FROM w.database
     AND h.relation IS NOT DISTINCT FROM w.relation
     AND h.page IS NOT DISTINCT FROM w.page
     AND h.tuple IS NOT DISTINCT FROM w.tuple
     AND h.virtualxid IS NOT DISTINCT FROM w.virtualxid
     AND h.transactionid IS NOT DISTINCT FROM w.transactionid
     AND h.classid IS NOT DISTINCT FROM w.classid
     AND h.objid IS NOT DISTINCT FROM w.objid
     AND h.objsubid IS NOT DISTINCT FROM w.objsubid
    WHERE NOT w.granted
"""

_NODE_FIELDS = (
    "datname",
    "usename",
    "application_name",
    "backend_type",
    "state",
    "wait_event_type",
    "wait_event",
    "state_seconds",
    "query_seconds",
    "transaction_seconds",
    "in_transaction",
    "query",
)


def assemble_lock_tree(backends: list[dict[str, Any]], holders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn the rows of the two lock-tree queries into nested trees.

    Args:
        backends: One row per backend involved, with ``blocked_by`` and the
            lock it waits on, as :data:`LOCK_TREE_BACKENDS_SQL` returns them.
        holders: ``(waiter, holder, held_mode)`` rows from
            :data:`LOCK_TREE_HOLDERS_SQL`.

    Returns:
        The roots, as :meth:`ActivityInspector.lock_tree` describes them.
    """
    by_pid: dict[int, dict[str, Any]] = {int(row["pid"]): row for row in backends}

    held: dict[tuple[int, int], list[str]] = defaultdict(list)
    for row in holders:
        held[(int(row["waiter"]), int(row["holder"]))].append(row["held_mode"])

    waiters_of: dict[int, list[int]] = defaultdict(list)
    for pid, row in by_pid.items():
        for blocker in row.get("blocked_by") or []:
            waiters_of[int(blocker)].append(pid)

    def build(pid: int, parent: int | None, trail: frozenset[int]) -> dict[str, Any]:
        row = by_pid.get(pid, {"pid": pid})
        node: dict[str, Any] = {"pid": pid}
        node.update({field: row.get(field) for field in _NODE_FIELDS})
        node["blocked_by"] = [int(p) for p in row.get("blocked_by") or []]
        node["waits_for"] = _waits_for(row, parent, sorted(set(held.get((pid, parent), [])))) if parent is not None else None

        children: list[dict[str, Any]] = []
        # The longest-running waiter first: it has been stuck the longest.
        for child in sorted(waiters_of.get(pid, []), key=lambda c: -(by_pid[c].get("query_seconds") or 0)):
            if child == pid or child in trail:
                node["cycle"] = True
                continue
            children.append(build(child, pid, trail | {pid}))
        node["blocking"] = children
        node["blocked_total"] = len(_pids_beneath(children))
        return node

    roots = [pid for pid, row in by_pid.items() if not row.get("blocked_by") and waiters_of.get(pid)]
    trees = [build(pid, None, frozenset()) for pid in roots]

    # A waiter no root reaches is in a cycle: every path from it leads back
    # to itself. The server will break the deadlock; until then, report it.
    reached = {pid for tree in trees for pid in _pids_beneath([tree])}
    for pid in by_pid:
        if pid not in reached and by_pid[pid].get("blocked_by"):
            tree = build(pid, None, frozenset())
            tree["cycle"] = True
            trees.append(tree)
            reached |= _pids_beneath([tree])

    trees.sort(key=lambda t: (-t["blocked_total"], -(t.get("transaction_seconds") or 0), t["pid"]))
    return trees


def _pids_beneath(nodes: list[dict[str, Any]]) -> set[int]:
    found: set[int] = set()
    for node in nodes:
        found.add(node["pid"])
        found |= _pids_beneath(node["blocking"])
    return found


def _waits_for(row: dict[str, Any], holder: int | None, held_modes: list[str]) -> dict[str, Any]:
    """Describe the lock a waiter is waiting on, in words and in fields."""
    locktype = row.get("waiting_locktype")
    mode = row.get("waiting_mode")
    oid = row.get("waiting_relation_oid")
    relation = row.get("waiting_relation") or (str(oid) if oid is not None else None)
    tuple_relation = row.get("tuple_relation")
    transactionid = row.get("waiting_transactionid")

    if locktype == "relation":
        description = f"{mode} on {relation}"
    elif locktype == "transactionid":
        description = f"transaction {transactionid} to end"
        if tuple_relation:
            description += f", to lock a row in {tuple_relation}"
    elif locktype == "tuple":
        description = f"a row lock in {relation}"
    elif locktype == "virtualxid":
        description = "a virtual transaction to end"
    elif locktype == "advisory":
        description = f"{mode} advisory lock"
    elif locktype is None:
        description = "a lock pg_locks no longer shows"
    else:
        description = f"{mode} {locktype} lock"

    if held_modes and holder is not None:
        description += f" ({holder} holds {' and '.join(held_modes)})"

    return {
        "locktype": locktype,
        "mode": mode,
        "relation": relation,
        "tuple_relation": tuple_relation,
        "transactionid": transactionid,
        "held_modes": held_modes,
        "description": description,
    }


__all__ = ["ActivityInspector", "assemble_lock_tree"]
