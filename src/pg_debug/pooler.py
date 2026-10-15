"""PgBouncer as its admin console reports it: are clients waiting for a server connection?

A transaction pooler hides saturation from PostgreSQL. A client waiting for
one of the pool's server connections is not a backend yet, so
``pg_stat_activity`` cannot show it, and from the database everything looks
quiet while the application times out. PgBouncer counts its waiting clients,
and how long the oldest has waited, in its admin console: the database named
``pgbouncer`` on the pooler's port, open to the ``admin_users`` and
``stats_users`` of its configuration.

The admin console is not PostgreSQL. It runs its own ``SHOW`` commands, only
over the simple query protocol, and accepts no transaction control, so it is
reached on a connection of its own rather than through
:class:`~pg_debug.db.SqlDriver`: the driver wraps every statement in a
transaction with its settings, marks it with a comment, and in restricted mode
parses it as PostgreSQL's grammar, none of which the console accepts. What
keeps it read-only instead is that pg-debug sends it nothing but the fixed
``SHOW`` commands in :data:`SHOW_COMMANDS`, and refuses any other before it is
sent. Each reads PgBouncer's state; none changes it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import Any
from typing import LiteralString

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .db import SafeSqlDriver
from .db import SqlDriver
from .db import get_postgres_version
from .db import obfuscate_password
from .db.driver import CONNECT_TIMEOUT
from .errors import ConnectionFailedError
from .errors import InvalidArgumentError
from .errors import PgDebugError
from .errors import QueryError
from .severity import SEVERITY_ORDER
from .severity import Severity

logger = logging.getLogger(__name__)

#: The admin console's database name.
ADMIN_DATABASE = "pgbouncer"

#: Every command pg-debug sends the admin console. Each only reads.
SHOW_COMMANDS: tuple[LiteralString, ...] = ("SHOW VERSION", "SHOW POOLS", "SHOW STATS", "SHOW DATABASES", "SHOW CONFIG")
_READ_ONLY = re.compile(r"^SHOW [A-Z_]+$")

#: The settings that decide how many clients and server connections there are.
CONFIG_KEYS = (
    "pool_mode",
    "max_client_conn",
    "default_pool_size",
    "min_pool_size",
    "reserve_pool_size",
    "max_db_connections",
    "max_user_connections",
)

#: A client that has waited this long for a server connection is waiting on a
#: full pool, not on one being opened.
MAXWAIT_HIGH_SECONDS = 1.0
#: The average wait, over PgBouncer's last stats_period, worth reporting, and
#: the one that is a problem, in microseconds as SHOW STATS gives it.
AVG_WAIT_NOTICE_US = 1_000
AVG_WAIT_MEDIUM_US = 100_000

#: The columns of SHOW POOLS that count server connections, whatever their state.
_SERVER_STATES = ("sv_active", "sv_idle", "sv_used", "sv_tested", "sv_login", "sv_active_cancel", "sv_being_canceled")


@dataclass(frozen=True)
class PoolerFinding:
    """One sign of saturation, with the counters it was read from.

    Attributes:
        kind: ``clients_waiting`` (clients are waiting for a server connection
            now), ``clients_waited`` (they waited, on average, over PgBouncer's
            last stats period) or ``pool_exceeds_server`` (the pools can open
            more server connections than the server accepts).
        severity: How much it matters.
        subject: The pool (``database/user``), the database, or the server.
        summary: What was found, in a sentence.
        evidence: The counters, as the admin console or the server gave them.
    """

    kind: str
    severity: Severity
    subject: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["severity"] = self.severity.value
        return result


@dataclass(frozen=True)
class PoolerReport:
    """What PgBouncer's admin console showed, and the saturation it shows.

    Attributes:
        version: PgBouncer's version string.
        pools: SHOW POOLS, one dict per database and user, with ``maxwait``
            in seconds (its ``maxwait_us`` folded in). The admin console's own
            pool is left out.
        stats: SHOW STATS, one dict per database; the ``avg_*`` times are in
            microseconds over PgBouncer's last ``stats_period``.
        databases: SHOW DATABASES: each database's ``pool_size``,
            ``reserve_pool`` and ``max_connections``.
        config: The settings of :data:`CONFIG_KEYS` from SHOW CONFIG.
        server: The PostgreSQL server's ``max_connections``,
            ``superuser_reserved_connections``, ``reserved_connections``
            (16+) and how many client backends it has, read through the
            database URL; None when it was not read.
        server_refused: Why ``server`` was not read, when it was not.
        unavailable: The SHOW commands the console refused, with its reason.
        findings: The signs of saturation, the most severe first. Empty when
            there are none: ``pools`` says what was examined.
    """

    version: str | None
    pools: list[dict[str, Any]]
    stats: list[dict[str, Any]]
    databases: list[dict[str, Any]]
    config: dict[str, Any]
    server: dict[str, Any] | None
    findings: list[PoolerFinding]
    server_refused: str | None = None
    unavailable: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["findings"] = [finding.to_dict() for finding in self.findings]
        return result


def _plain(value: Any) -> Any:
    """A console value as JSON-friendly data: its counters come back as Decimal."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def check_admin_url(admin_url: str) -> None:
    """Refuse a URL that does not name the admin console.

    Raises:
        InvalidArgumentError: If its database is not ``pgbouncer``, or it
            cannot be parsed.
    """
    try:
        database = conninfo_to_dict(admin_url).get("dbname")
    except psycopg.ProgrammingError as e:
        raise InvalidArgumentError(f"Not a connection URL: {obfuscate_password(str(e))}") from e
    if database != ADMIN_DATABASE:
        raise InvalidArgumentError(
            f"PgBouncer's admin console is the database named {ADMIN_DATABASE} on the pooler's port, as in "
            f"postgresql://user@host:6432/{ADMIN_DATABASE}; this URL names {database or 'no database'}. "
            "The user has to be in admin_users or stats_users in pgbouncer.ini."
        )


async def _show(connection: psycopg.AsyncConnection[Any], command: LiteralString) -> list[dict[str, Any]]:
    if command not in SHOW_COMMANDS or not _READ_ONLY.match(command):
        raise QueryError(f"pg-debug sends the admin console only {', '.join(SHOW_COMMANDS)}; refused {command!r}.")
    cursor = await connection.execute(command)
    names = [column.name for column in cursor.description or []]
    return [{name: _plain(value) for name, value in zip(names, row, strict=True)} for row in await cursor.fetchall()]


async def read_admin_console(admin_url: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Every SHOW command's rows, and the ones the console refused with its reason.

    Connected with autocommit, as the console has no transactions, and with
    client-side binding and no prepared statements, which keep psycopg on the
    simple query protocol, the only one the console speaks.

    Raises:
        InvalidArgumentError: If the URL is not the admin console's.
        ConnectionFailedError: If the console could not be reached.
    """
    check_admin_url(admin_url)
    extra: dict[str, Any] = {} if "connect_timeout" in conninfo_to_dict(admin_url) else {"connect_timeout": CONNECT_TIMEOUT}
    try:
        connection = await psycopg.AsyncConnection.connect(
            admin_url,
            autocommit=True,
            prepare_threshold=None,
            cursor_factory=psycopg.AsyncClientCursor,
            **extra,
        )
    except psycopg.Error as e:
        raise ConnectionFailedError(f"Could not connect to PgBouncer's admin console: {obfuscate_password(str(e))}") from e

    results: dict[str, list[dict[str, Any]]] = {}
    refused: dict[str, str] = {}
    async with connection:
        for command in SHOW_COMMANDS:
            try:
                results[command] = await _show(connection, command)
            except psycopg.Error as e:
                refused[command] = str(e).strip()
    return results, refused


#: reserved_connections, kept for pg_use_reserved_connections, came with 16.
RESERVED_CONNECTIONS_MIN_VERSION = 16


async def read_server_connections(sql_driver: SqlDriver | SafeSqlDriver) -> dict[str, Any]:
    """The server's ``max_connections``, the connections it keeps for privileged roles, and its client backends now."""
    reserved: LiteralString = "0"
    if await get_postgres_version(sql_driver) >= RESERVED_CONNECTIONS_MIN_VERSION:
        reserved = "current_setting('reserved_connections')::int"
    query: LiteralString = f"""
        SELECT
            current_setting('max_connections')::int AS max_connections,
            current_setting('superuser_reserved_connections')::int AS superuser_reserved_connections,
            {reserved} AS reserved_connections,
            (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend')::int AS client_backends
    """
    rows = await SafeSqlDriver.execute_param_query(sql_driver, query)
    return dict(rows[0].cells) if rows else {}


def assess(
    pools: list[dict[str, Any]],
    stats: list[dict[str, Any]],
    databases: list[dict[str, Any]],
    config: dict[str, Any],
    server: dict[str, Any] | None,
) -> list[PoolerFinding]:
    """The signs of saturation in what the console showed, the most severe first."""
    findings: list[PoolerFinding] = []
    by_name = {db.get("name"): db for db in databases}

    for pool in pools:
        waiting = pool.get("cl_waiting") or 0
        if not waiting:
            continue
        maxwait = pool.get("maxwait") or 0.0
        subject = f"{pool.get('database')}/{pool.get('user')}"
        database = by_name.get(pool.get("database"), {})
        severity = Severity.HIGH if maxwait >= MAXWAIT_HIGH_SECONDS else Severity.MEDIUM
        findings.append(
            PoolerFinding(
                kind="clients_waiting",
                severity=severity,
                subject=subject,
                summary=(
                    f"{waiting} client{'s are' if waiting != 1 else ' is'} waiting for a server connection in pool {subject}, "
                    f"the oldest for {maxwait:.3f}s, with {pool.get('sv_active') or 0} server connection(s) busy and "
                    f"{pool.get('sv_idle') or 0} idle (pool_size {database.get('pool_size', config.get('default_pool_size'))})."
                ),
                evidence={
                    key: pool.get(key) for key in ("cl_active", "cl_waiting", "sv_active", "sv_idle", "sv_used", "sv_login", "maxwait", "pool_mode")
                }
                | {"pool_size": database.get("pool_size"), "reserve_pool": database.get("reserve_pool")},
            )
        )

    for row in stats:
        if row.get("database") == ADMIN_DATABASE:
            continue
        wait = row.get("avg_wait_time") or 0
        if wait < AVG_WAIT_NOTICE_US:
            continue
        findings.append(
            PoolerFinding(
                kind="clients_waited",
                severity=Severity.MEDIUM if wait >= AVG_WAIT_MEDIUM_US else Severity.LOW,
                subject=str(row.get("database")),
                summary=(
                    f"Clients of {row.get('database')} waited {wait / 1000:,.1f}ms on average for a server connection over "
                    f"PgBouncer's last stats period, against {(row.get('avg_xact_time') or 0) / 1000:,.1f}ms for an average transaction."
                ),
                evidence={key: row.get(key) for key in ("avg_wait_time", "avg_xact_time", "avg_query_time", "avg_xact_count", "total_wait_time")},
            )
        )

    if server and server.get("max_connections"):
        finding = _pools_against_server(pools, databases, config, server)
        if finding is not None:
            findings.append(finding)

    findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return findings


def _pools_against_server(
    pools: list[dict[str, Any]], databases: list[dict[str, Any]], config: dict[str, Any], server: dict[str, Any]
) -> PoolerFinding | None:
    """Whether the pools can open more server connections than the server accepts for ordinary roles."""
    by_name = {db.get("name"): db for db in databases}
    users: dict[Any, int] = {}
    for pool in pools:
        users[pool.get("database")] = users.get(pool.get("database"), 0) + 1

    possible = 0
    for name, count in users.items():
        database = by_name.get(name, {})
        size = database.get("pool_size")
        each = int(size if size is not None else config.get("default_pool_size") or 0)
        each += int(database.get("reserve_pool") or 0)
        cap = each * count
        if database.get("max_connections"):
            cap = min(cap, int(database["max_connections"]))
        possible += cap

    open_now = sum(sum(int(pool.get(state) or 0) for state in _SERVER_STATES) for pool in pools)
    available = int(server["max_connections"]) - int(server.get("superuser_reserved_connections") or 0) - int(server.get("reserved_connections") or 0)
    if possible <= available:
        return None
    return PoolerFinding(
        kind="pool_exceeds_server",
        severity=Severity.MEDIUM,
        subject="server",
        summary=(
            f"PgBouncer's {len(pools)} pool(s) can open {possible} server connections, and the server accepts {available} "
            f"from ordinary roles (max_connections {server['max_connections']} less the reserved ones), if every pool reaches "
            f"this server. Once they are all in use, a new connection is refused with 'too many clients' instead of waiting "
            f"in the pool. It has {server.get('client_backends')} client backends now, {open_now} of them PgBouncer's."
        ),
        evidence={"pool_connections_possible": possible, "server_connections_open": open_now, "server_accepts": available, **server},
    )


async def inspect_pgbouncer(
    admin_url: str,
    read_server: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    server_refused: str | None = None,
) -> PoolerReport:
    """Read PgBouncer's admin console and report the saturation it shows.

    Args:
        admin_url: The admin console: ``postgresql://user@host:6432/pgbouncer``,
            as a user in ``admin_users`` or ``stats_users``.
        read_server: Reads the PostgreSQL server's connection limits, as
            :func:`read_server_connections` does, to compare the pool sizes
            with; None to leave that out. Called after the console is read: a
            connection through a full pool waits in it like every other
            client, and the console is read before pg-debug joins the queue.
            A :class:`~pg_debug.errors.PgDebugError` it raises is reported in
            ``server_refused``.
        server_refused: Why there is no ``read_server``, to say in the report.

    Raises:
        InvalidArgumentError: If the URL is not the admin console's.
        ConnectionFailedError: If the console could not be reached.
    """
    results, unavailable = await read_admin_console(admin_url)
    server: dict[str, Any] | None = None
    if read_server is not None:
        try:
            server = await read_server()
        except PgDebugError as e:
            server_refused = f"the server's max_connections could not be read: {obfuscate_password(str(e))}"

    pools = []
    for row in results.get("SHOW POOLS", []):
        if row.get("database") == ADMIN_DATABASE:
            continue
        # maxwait is whole seconds and maxwait_us the microseconds beyond them.
        row["maxwait"] = (row.get("maxwait") or 0) + (row.pop("maxwait_us", 0) or 0) / 1_000_000
        pools.append(row)
    stats = [row for row in results.get("SHOW STATS", []) if row.get("database") != ADMIN_DATABASE]
    databases = [row for row in results.get("SHOW DATABASES", []) if row.get("name") != ADMIN_DATABASE]
    config = {row["key"]: row.get("value") for row in results.get("SHOW CONFIG", []) if row.get("key") in CONFIG_KEYS}
    for key, value in config.items():
        if isinstance(value, str) and value.isdigit():
            config[key] = int(value)
    version_rows = results.get("SHOW VERSION", [])
    version = str(next(iter(version_rows[0].values()))) if version_rows else None

    return PoolerReport(
        version=version,
        pools=pools,
        stats=stats,
        databases=databases,
        config=config,
        server=server,
        server_refused=server_refused,
        findings=assess(pools, stats, databases, config, server),
        unavailable=unavailable,
    )


__all__ = [
    "ADMIN_DATABASE",
    "SHOW_COMMANDS",
    "PoolerFinding",
    "PoolerReport",
    "assess",
    "check_admin_url",
    "inspect_pgbouncer",
    "read_admin_console",
    "read_server_connections",
]
