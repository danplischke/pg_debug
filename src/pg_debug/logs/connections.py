"""Connections the server refused, ended or lost, as its log records them.

None of these leave more than a counter in the statistics views, and most not
even that: a client that gave the wrong password, a pool that ran into
``max_connections``, a session ended by ``idle_in_transaction_session_timeout``,
a backend the kernel's OOM killer took and the restart of every other session
that followed. The log names the role, the database and, for a refusal by
``pg_hba.conf``, the host.

The messages are matched in English, as PostgreSQL writes them with
``lc_messages`` set to C or English.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from .parse import LogEntry

#: How many ``file:line`` references a kind of event keeps.
MAX_SOURCES = 5


@dataclass(frozen=True)
class _Kind:
    name: str
    pattern: re.Pattern[str]
    levels: tuple[str, ...]
    meaning: str


#: Each kind of connection event: the message that starts it, the severities
#: it is logged at, and what it means. The first that matches wins.
KINDS: tuple[_Kind, ...] = (
    _Kind(
        "authentication_failed",
        re.compile(r'^(?:(?P<method>[\w-]+) )?authentication failed for user "(?P<user>[^"]*)"'),
        ("FATAL",),
        "A client could not authenticate: a wrong or expired password, or a credential the method rejected. A steady "
        "stream from one role is usually a deploy with a stale secret; from many roles, someone guessing.",
    ),
    _Kind(
        "no_hba_entry",
        re.compile(r'^no pg_hba\.conf entry for host "(?P<host>[^"]*)", user "(?P<user>[^"]*)", database "(?P<database>[^"]*)"'),
        ("FATAL",),
        "No pg_hba.conf line matches this host, role and database (and encryption), so the connection was refused before any password was asked for.",
    ),
    _Kind(
        "hba_rejected",
        re.compile(r'^pg_hba\.conf rejects connection for host "(?P<host>[^"]*)", user "(?P<user>[^"]*)", database "(?P<database>[^"]*)"'),
        ("FATAL",),
        "A pg_hba.conf line with the reject method matched this connection.",
    ),
    _Kind(
        "unknown_role",
        re.compile(r'^role "(?P<user>[^"]*)" does not exist'),
        ("FATAL",),
        "A client connected as a role that does not exist: a typo in a connection string, or a role dropped while something still used it.",
    ),
    _Kind(
        "unknown_database",
        re.compile(r'^database "(?P<database>[^"]*)" does not exist'),
        ("FATAL",),
        "A client asked for a database that does not exist.",
    ),
    _Kind(
        "too_many_clients",
        re.compile(r"^(?:sorry, too many clients already|remaining connection slots are reserved for)"),
        ("FATAL",),
        "max_connections was reached, and the connection was refused. A pooler in front of the server, or a smaller pool "
        "per application instance, keeps the count below it.",
    ),
    _Kind(
        "connection_limit",
        re.compile(r'^too many connections for (?:role|database) "(?P<name>[^"]*)"'),
        ("FATAL",),
        "The role's or the database's CONNECTION LIMIT was reached.",
    ),
    _Kind(
        "not_accepting",
        re.compile(r"^the database system is (?:starting up|shutting down|in recovery mode|not yet accepting connections|not accepting connections)"),
        ("FATAL",),
        "The server was starting, stopping or recovering, and refused connections meanwhile.",
    ),
    _Kind(
        "idle_in_transaction_timeout",
        re.compile(r"^terminating connection due to idle-in-transaction timeout"),
        ("FATAL",),
        "idle_in_transaction_session_timeout ended a session that held a transaction open and did nothing with it. The "
        "transaction was rolled back: the application lost whatever it had not committed.",
    ),
    _Kind(
        "idle_session_timeout",
        re.compile(r"^terminating connection due to idle-session timeout"),
        ("FATAL",),
        "idle_session_timeout ended a session that sat idle outside a transaction.",
    ),
    _Kind(
        "terminated_by_administrator",
        re.compile(r"^terminating connection due to administrator command"),
        ("FATAL",),
        "pg_terminate_backend() or a fast shutdown ended the session.",
    ),
    _Kind(
        "recovery_conflict",
        re.compile(r"^(?:terminating connection|canceling statement) due to conflict with recovery"),
        ("FATAL", "ERROR"),
        "On a standby, replaying WAL needed something this session held, and max_standby_streaming_delay ran out. "
        "hot_standby_feedback, or a longer delay, trades this for bloat or lag on the primary.",
    ),
    _Kind(
        "backend_crashed",
        # A death by signal, or an exit code a clean exit does not use: a
        # background worker exiting with 1 is how a fast shutdown stops it.
        re.compile(
            r"^(?:server process|background worker \"[^\"]*\"|[\w ]+?) \(PID (?P<pid>\d+)\) "
            r"(?:was terminated by signal (?P<signal>\d+)|exited with exit code (?P<code>(?![01]\b)\d+))"
        ),
        ("LOG",),
        "A server process died, and the postmaster restarted every session to protect shared memory. Signal 9 (Killed) "
        "is almost always the kernel's OOM killer: memory ran out, often from work_mem times the sorts and hashes running at once.",
    ),
    _Kind(
        "terminated_by_crash",
        re.compile(r"^terminating connection because of crash of another server process"),
        ("WARNING", "FATAL"),
        "This session was ended because another server process crashed; see backend_crashed.",
    ),
    _Kind(
        "ssl_failed",
        re.compile(r"^(?:could not accept SSL connection|SSL error|connection requires a valid client certificate)"),
        ("LOG", "FATAL", "COMMERROR"),
        "The TLS handshake failed: a client that does not trust the server's certificate, an old TLS version, or a "
        "client certificate the server requires and did not get.",
    ),
    _Kind(
        "client_vanished_in_transaction",
        re.compile(r"^unexpected EOF on client connection with an open transaction"),
        ("LOG", "FATAL"),
        "A client disconnected in the middle of a transaction, which was rolled back. Its process crashed, or a "
        "network device dropped the idle connection.",
    ),
    _Kind(
        "client_reset",
        re.compile(r"^could not receive data from client: Connection reset by peer"),
        ("LOG",),
        "A client's connection was reset without a goodbye: the client was killed, or something between it and the "
        "server (a load balancer, a NAT, a firewall) dropped the connection.",
    ),
)


@dataclass
class ConnectionEvents:
    """Every logged occurrence of one kind of connection event.

    Attributes:
        kind: One of :data:`KINDS`' names: ``authentication_failed``,
            ``too_many_clients``, ``backend_crashed``...
        meaning: What it means, and the usual cause.
        count: How many times it was logged.
        by: How many times per ``user@database`` (or host), where the message
            or the log line prefix names them; most first.
        example: One message, as logged.
        details: For ``backend_crashed``, each signal or exit seen; for an
            authentication failure, the method where the message names it.
        first_time: When the first was logged.
        last_time: When the last was.
        sources: Up to five ``file:line`` references.
    """

    kind: str
    meaning: str
    count: int = 0
    by: dict[str, int] = field(default_factory=dict)
    example: str = ""
    details: dict[str, int] = field(default_factory=dict)
    first_time: str | None = None
    last_time: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "meaning": self.meaning,
            "count": self.count,
            "by": dict(sorted(self.by.items(), key=lambda item: -item[1])),
            "example": self.example,
            "details": self.details,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "sources": self.sources,
        }


class ConnectionReader:
    """Collects the connection events in a log's entries, by kind."""

    def __init__(self) -> None:
        self.events: dict[str, ConnectionEvents] = {}

    def read(self, entry: LogEntry) -> bool:
        """Take the entry if it is a connection event; say whether it was."""
        for kind in KINDS:
            if entry.level not in kind.levels:
                continue
            match = kind.pattern.match(entry.message)
            if match is None:
                continue
            self._add(kind, entry, match.groupdict())
            return True
        return False

    def _add(self, kind: _Kind, entry: LogEntry, fields: dict[str, Any]) -> None:
        events = self.events.get(kind.name)
        if events is None:
            events = ConnectionEvents(kind=kind.name, meaning=kind.meaning, example=entry.message.splitlines()[0], first_time=entry.time)
            self.events[kind.name] = events
        events.count += 1
        events.last_time = entry.time
        if len(events.sources) < MAX_SOURCES:
            events.sources.append(entry.source)
        user = fields.get("user") or entry.user
        database = fields.get("database") or entry.database
        who = fields.get("name") or (f"{user or '?'}@{database or '?'}" if user or database else None)
        if fields.get("host"):
            who = f"{who} from {fields['host']}" if who else fields["host"]
        if who:
            events.by[who] = events.by.get(who, 0) + 1
        detail = None
        if fields.get("signal"):
            detail = f"signal {fields['signal']}"
        elif fields.get("code"):
            detail = f"exit code {fields['code']}"
        elif fields.get("method"):
            detail = fields["method"]
        if detail:
            events.details[detail] = events.details.get(detail, 0) + 1

    def finish(self) -> list[ConnectionEvents]:
        """The events, the most frequent first."""
        return sorted(self.events.values(), key=lambda events: -events.count)


__all__ = ["KINDS", "ConnectionEvents", "ConnectionReader"]
