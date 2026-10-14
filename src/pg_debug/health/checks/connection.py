"""Connection use, judged against the server's own limit.

A count of connections means nothing without the limit it is counted against:
200 is a quarter of one server's slots and all of another's, and managed
platforms size ``max_connections`` to the compute, from tens to thousands. So
the count is judged as a share of the slots an ordinary role can use, which is
``max_connections`` less the slots reserved for superusers and, from
PostgreSQL 16, for roles granted ``pg_use_reserved_connections``. Only client
backends take those slots. The server's own processes, parallel workers and
WAL senders have limits of their own and are not counted.

Idle in transaction is judged by how long, not how many. A busy application
that makes a round trip between the statements of a transaction shows
sessions idle in transaction at any instant, each for a few milliseconds, and
harmlessly. What does harm is one left open: it keeps every lock it took
until it ends, even the share lock of a plain read (measured on PostgreSQL
16), which is enough to queue an ALTER TABLE and every query that arrives
after it. A READ COMMITTED transaction that has only read holds no snapshot
between statements, so the vacuum horizon check rightly does not report it;
this one does. A transaction idle after an error has already released its
locks, and only its connection is counted.

Behind a connection pooler the server sees the pooler's server connections,
not its clients. A pool that is too small queues clients the server never
sees, and this check cannot see that; the pooler's own statistics can.
"""

from __future__ import annotations

from typing import Any

import humanize

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ..result import HealthCheckResult

CONNECTION_HEALTH = "Connection health"

#: The ``kind`` of each finding, part of its identity in a baseline: accepting
#: a session left idle in a transaction must not accept running out of
#: connections, nor the other way round.
CONNECTIONS_IN_USE = "connections_in_use"
IDLE_IN_TRANSACTION = "idle_in_transaction"

#: Flag connection use from this share of the slots ordinary roles can use.
#: A judgment, not a measurement: a connection refused is an outage for
#: whoever asked, and connections arrive in steps rather than one at a time —
#: a deploy starts its new instances' pools before the old ones close — so the
#: warning should come while there is still room for one such step.
DEFAULT_MAX_IN_USE_SHARE = 0.8

#: Flag a session idle in a transaction for this long. Also a judgment: a
#: minute is well past any network round trip between two statements, so a
#: session idle that long is waiting on something outside the database — a
#: remote call, a user, a worker that died before committing — with its locks
#: held. Well short of the vacuum horizon check's fifteen minutes, because
#: locks do harm long before a held-back horizon does.
DEFAULT_MAX_IDLE_IN_TRANSACTION_SECONDS = 60

#: How much of an idle session's last statement a finding carries: enough to
#: recognise the code that left the transaction open, as the vacuum horizon
#: check carries for the transactions it names.
QUERY_CHARS = 200

# A role without pg_read_all_stats sees other roles' backends with most
# columns NULL, backend_type and state among them (measured on PostgreSQL 16).
# Those with a role and a database behind them may be client connections; the
# server's own processes have no role, and the logical replication launcher
# and a physical WAL sender, which have one, show no database (also measured
# on 16; without that, every such role read a range of one). They are counted
# apart, so that what this role could not see is said rather than read as
# zero. reserved_connections is new in 16 and reads NULL before it.
#
# One statement, reading pg_stat_activity once, so that the counts and the
# sessions listed agree. It returns one row per session idle in transaction
# past the limit, each carrying the counts, or a single row of counts and no
# session when there is none.
CONNECTIONS_SQL = """
WITH activity AS (
    SELECT pid, datid, datname, usesysid, usename, application_name, backend_type, state, state_change, query
    FROM pg_stat_activity
),
counts AS (
    SELECT
        count(*) FILTER (WHERE backend_type = 'client backend') AS client_connections,
        count(*) FILTER (WHERE backend_type IS NULL AND usesysid IS NOT NULL AND datid IS NOT NULL) AS hidden_backends,
        count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'idle in transaction') AS idle_in_transaction,
        extract(epoch FROM max(now() - state_change) FILTER (
            WHERE backend_type = 'client backend' AND state = 'idle in transaction'
        ))::bigint AS longest_idle_in_transaction_seconds,
        current_setting('max_connections')::int AS max_connections,
        current_setting('superuser_reserved_connections')::int AS superuser_reserved_connections,
        current_setting('reserved_connections', true)::int AS reserved_connections
    FROM activity
),
long_idle AS (
    SELECT
        pid,
        usename,
        datname,
        application_name,
        extract(epoch FROM now() - state_change)::bigint AS idle_in_transaction_seconds,
        left(query, {}) AS query
    FROM activity
    WHERE backend_type = 'client backend'
      AND state = 'idle in transaction'
      AND now() - state_change >= make_interval(secs => {})
)
SELECT counts.*, long_idle.*
FROM counts
LEFT JOIN long_idle ON true
ORDER BY long_idle.idle_in_transaction_seconds DESC NULLS LAST, long_idle.pid
"""


class ConnectionHealthCalc:
    def __init__(
        self,
        sql_driver: SqlDriver,
        max_in_use_share: float = DEFAULT_MAX_IN_USE_SHARE,
        max_idle_in_transaction_seconds: int = DEFAULT_MAX_IDLE_IN_TRANSACTION_SECONDS,
    ):
        self.sql_driver = sql_driver
        self.max_in_use_share = max_in_use_share
        self.max_idle_in_transaction_seconds = max_idle_in_transaction_seconds

    async def connection_health_check(self) -> HealthCheckResult:
        """Judge client connections against the slots ordinary roles can use.

        Unhealthy when client connections reach ``max_in_use_share`` of those
        slots, or when a session has sat idle in a transaction for
        ``max_idle_in_transaction_seconds`` or more. Each verdict is a finding
        that carries its evidence: one ``connections_in_use`` finding with the
        counts, and one ``idle_in_transaction`` finding per session, with its
        pid, role, database, application, how long it has been idle and the
        start of its last statement. When other roles' backends are hidden
        from this role and could tip the count either way, it says it cannot
        tell rather than counting them as nothing, and it claims nothing about
        sessions it could not see.
        """
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, CONNECTIONS_SQL, [QUERY_CHARS, self.max_idle_in_transaction_seconds])
        row: dict[str, Any] = dict(rows[0].cells) if rows else {}
        sessions = [_idle_session(r.cells) for r in rows or [] if r.cells.get("pid") is not None]

        client = int(row.get("client_connections") or 0)
        hidden = int(row.get("hidden_backends") or 0)
        max_connections = int(row.get("max_connections") or 0)
        superuser_reserved = int(row.get("superuser_reserved_connections") or 0)
        reserved = row.get("reserved_connections")
        available = max_connections - superuser_reserved - int(reserved or 0)

        threshold = self.max_in_use_share * available
        in_use: dict[str, Any] = {
            "client_connections": client,
            "max_connections": max_connections,
            "superuser_reserved_connections": superuser_reserved,
            "reserved_connections": reserved,
            "available_connections": available,
            "in_use_percent": round(100 * client / available, 1) if available > 0 else None,
            "max_in_use_percent": round(100 * self.max_in_use_share, 1),
        }
        metrics = {
            **in_use,
            "idle_in_transaction": int(row.get("idle_in_transaction") or 0),
            "long_idle_in_transaction": len(sessions),
            "longest_idle_in_transaction_seconds": row.get("longest_idle_in_transaction_seconds"),
            "max_idle_in_transaction_seconds": self.max_idle_in_transaction_seconds,
            "hidden_backends": hidden,
        }

        between = f"{client:,}" if not hidden else f"between {client:,} and {client + hidden:,}"
        reservations = _reservations(max_connections, superuser_reserved, reserved)
        slots = f"the {available:,} slots ordinary roles can use ({reservations})"
        idle_limit = humanize.naturaldelta(self.max_idle_in_transaction_seconds)
        lines: list[str] = []
        findings: list[dict[str, Any]] = []

        if available <= 0:
            # PostgreSQL refuses to start like this, so it is a reading gone
            # wrong, not a server without room: judge nothing from it.
            lines.append(f"Connection use not judged: no slot is left for ordinary roles ({reservations}).")
        elif client >= threshold:
            findings.append({"kind": CONNECTIONS_IN_USE, **in_use})
            lines.append(
                f"High number of connections: {client:,} client connections use {client / available:.0%} of {slots}, "
                f"flagged from {self.max_in_use_share:.0%}."
            )
        elif client + hidden >= threshold:
            lines.append(
                f"Connection use cannot be judged: {between} client connections of {slots}, "
                f"against the {self.max_in_use_share:.0%} it is flagged from."
            )

        if sessions:
            findings.extend(sessions)
            lines.append(
                f"{_count(len(sessions), 'connection')} idle in transaction for {idle_limit} or more, "
                f"{'' if len(sessions) == 1 else 'each '}holding its locks and its connection until it ends:"
            )
            lines.extend(_describe_idle(session) for session in sessions)

        if not lines:
            # Another role's session shows this one no state at all, so the
            # claim stops at what could be seen.
            none_idle = "none of the visible ones" if hidden else "none"
            lines.append(f"Connections healthy: {between} client connections of {slots}, {none_idle} idle in transaction for {idle_limit} or more.")

        if hidden == 1:
            lines.append(
                "1 backend of another role hides its type and state from this role, so it was counted only as a possible "
                "client connection and not judged for idle in transaction; pg_read_all_stats, which pg_monitor includes, shows it."
            )
        elif hidden:
            lines.append(
                f"{hidden:,} backends of other roles hide their type and state from this role, so they were counted only as "
                "possible client connections and not judged for idle in transaction; pg_read_all_stats, which pg_monitor "
                "includes, shows them."
            )

        return HealthCheckResult(
            name=CONNECTION_HEALTH,
            healthy=not findings,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )


def _idle_session(cells: dict[str, Any]) -> dict[str, Any]:
    """A session idle in transaction past the limit, as a finding.

    Its pid is evidence, not identity: the same job that leaves a transaction
    open comes back under a new pid every time it reconnects, so a baseline
    knows it by its role, database and application name.
    """
    return {
        "kind": IDLE_IN_TRANSACTION,
        "pid": cells.get("pid"),
        "usename": cells.get("usename"),
        "datname": cells.get("datname"),
        "application_name": cells.get("application_name"),
        "idle_in_transaction_seconds": cells.get("idle_in_transaction_seconds"),
        "query": cells.get("query"),
    }


def _describe_idle(session: dict[str, Any]) -> str:
    who = [f"role {session['usename']}", f"database {session['datname']}"]
    if session.get("application_name"):
        who.append(f"application {session['application_name']}")
    idle = humanize.naturaldelta(int(session.get("idle_in_transaction_seconds") or 0))
    query = str(session.get("query") or "")
    truncated = "…" if len(query) >= QUERY_CHARS else ""
    query = " ".join(query.split())
    last = f"; its last statement: {query}{truncated}" if query else ""
    return f"Backend {session['pid']} ({', '.join(who)}) has been idle in transaction for {idle}{last}"


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _reservations(max_connections: int, superuser_reserved: int, reserved: Any) -> str:
    """How the usable slots were arrived at, in the server's own terms."""
    parts = [f"max_connections {max_connections:,}", f"less {superuser_reserved:,} superuser_reserved_connections"]
    if reserved:
        parts.append(f"less {int(reserved):,} reserved_connections")
    return ", ".join(parts)


__all__ = [
    "CONNECTIONS_IN_USE",
    "CONNECTION_HEALTH",
    "DEFAULT_MAX_IDLE_IN_TRANSACTION_SECONDS",
    "DEFAULT_MAX_IN_USE_SHARE",
    "IDLE_IN_TRANSACTION",
    "ConnectionHealthCalc",
]
