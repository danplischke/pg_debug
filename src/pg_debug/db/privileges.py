"""What the statistics views show the connecting role, and what they withhold from it.

PostgreSQL lists every backend in ``pg_stat_activity`` and every statement in
``pg_stat_statements`` to every role, but withholds the details of other
roles' unless the reader has the privileges of ``pg_read_all_stats``, which
``pg_monitor`` includes and a superuser has (before PostgreSQL 15, unless it
is a member, inheriting or not):

- A withheld backend keeps its pid, database, user and application, and its
  transaction ID and snapshot (``backend_xid``, ``backend_xmin``). Its state,
  query, wait and timings are NULL, and its query reads
  :data:`HIDDEN_QUERY_TEXT`. The server's own processes are withheld the same
  way. ``pg_locks`` withholds nothing, so the locks a withheld backend holds
  and waits for, and whether it is in a transaction, can still be read.
- A withheld statement keeps its user, database and counters. Its queryid is
  NULL and its text reads :data:`HIDDEN_QUERY_TEXT`.

The readers of those views return what the server showed, as plain rows. The
functions here say what it withheld, and why, for the caller to tell its user.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import LiteralString

from .driver import SqlDriver
from .extensions import get_postgres_version
from .restricted import SafeSqlDriver

#: What ``pg_stat_activity`` and ``pg_stat_statements`` show in place of the
#: query text of a backend or a statement the reading role may not see.
HIDDEN_QUERY_TEXT = "<insufficient privilege>"

#: From PostgreSQL 15 the statistics views ask whether the role has the
#: privileges of pg_read_all_stats, which a member that does not inherit them
#: lacks; before, whether it is a member at all.
PRIVILEGES_OF_ROLE_MIN_VERSION = 15

#: From PostgreSQL 16 whether a member inherits a role's privileges is an
#: option of each grant, ``pg_auth_members.inherit_option``, and the role's own
#: INHERIT attribute is only the default for grants made after it is set.
GRANT_INHERIT_OPTION_MIN_VERSION = 16

#: Whether a backend is in a transaction, from what PostgreSQL shows every
#: role: a transaction ID, a snapshot, or the lock every transaction holds on
#: its own virtual transaction ID from its first statement to its end. Reads
#: ``pg_stat_activity`` as ``a``, and :data:`IN_TRANSACTION_CTE` as
#: ``in_transaction``.
IN_TRANSACTION = """(
    a.xact_start IS NOT NULL
    OR a.backend_xid IS NOT NULL
    OR a.backend_xmin IS NOT NULL
    OR a.pid IN (SELECT pid FROM in_transaction)
)"""

#: The backends holding the lock on their own virtual transaction, which is to
#: say the ones in a transaction. For :data:`IN_TRANSACTION`.
IN_TRANSACTION_CTE = """in_transaction AS (
    SELECT l.pid
    FROM pg_locks l
    WHERE l.locktype = 'virtualxid'
      AND l.granted
      AND l.virtualxid = l.virtualtransaction
)"""

#: Every backend whose details PostgreSQL withheld from the reading role, with
#: what it still shows of each: whether it is in a transaction, how many
#: transactions old its snapshot is, the locks it holds besides the ones every
#: transaction holds on itself, and which backends wait on it. A lock on a
#: relation is named when it is in this database or shared; another
#: database's is given by OID, since this catalog cannot name it.
WITHHELD_BACKENDS_SQL = f"""
    WITH {IN_TRANSACTION_CTE},
    held AS (
        SELECT
            l.pid,
            array_agg(
                DISTINCT l.mode || ' on ' || CASE
                    WHEN l.locktype = 'advisory' THEN 'advisory lock ' || CASE
                        WHEN l.classid = 0 AND l.objsubid = 1 THEN l.objid::text
                        ELSE l.classid::text || ', ' || l.objid::text
                    END
                    WHEN l.relation IS NULL THEN l.locktype
                    ELSE CASE WHEN l.locktype = 'relation' THEN '' ELSE l.locktype || ' of ' END || coalesce(
                        quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                        'relation ' || l.relation::text || ' of another database'
                    )
                END
            ) AS locks
        FROM pg_locks l
        LEFT JOIN pg_class c
          ON c.oid = l.relation
         AND (l.database = 0 OR l.database = (SELECT d.oid FROM pg_database d WHERE d.datname = current_database()))
        LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE l.granted
          AND l.pid IS NOT NULL
          AND l.locktype NOT IN ('virtualxid', 'transactionid')
        GROUP BY l.pid
    ),
    waiting AS (
        SELECT DISTINCT l.pid FROM pg_locks l WHERE NOT l.granted AND l.pid IS NOT NULL
    ),
    blocked AS (
        SELECT w.pid AS waiter, unnest(pg_blocking_pids(w.pid)) AS blocker FROM waiting w
    )
    SELECT
        a.pid,
        a.datname,
        a.usename,
        a.application_name,
        {IN_TRANSACTION} AS in_transaction,
        age(a.backend_xmin) AS xmin_age,
        coalesce(h.locks, ARRAY[]::text[]) AS locks,
        ARRAY(SELECT DISTINCT b.waiter FROM blocked b WHERE b.blocker = a.pid ORDER BY b.waiter) AS blocking
    FROM pg_stat_activity a
    LEFT JOIN held h ON h.pid = a.pid
    WHERE a.pid <> pg_backend_pid()
      AND a.query = '{HIDDEN_QUERY_TEXT}'
    ORDER BY a.pid
"""


@dataclass(frozen=True)
class StatisticsAccess:
    """Whether the connecting role is shown every role's activity and statements, and how it could be.

    Attributes:
        role: The role the statistics views are read as, ``current_user``.
        reads_all_stats: Whether it has what the server's version asks
            before it shows another role's row: the privileges of
            ``pg_read_all_stats`` from PostgreSQL 15, membership before. A
            superuser always has.
        member: Whether it is a member of ``pg_read_all_stats``, directly or
            through ``pg_monitor``. A member without ``reads_all_stats`` does
            not inherit the role's privileges, which PostgreSQL 15 and later
            ask for: granting it ``pg_monitor`` again would change nothing.
        remedy: The statement that would show it everything, or None when it
            is shown everything already: a grant of ``pg_monitor``, or for a
            member that does not inherit it, the grant made to inherit
            (PostgreSQL 16 and later) or the role made to inherit (15).
    """

    role: str
    reads_all_stats: bool
    member: bool = False
    remedy: str | None = None


def _access_sql(version: int) -> LiteralString:
    privilege = "USAGE" if version >= PRIVILEGES_OF_ROLE_MIN_VERSION else "MEMBER"
    # The grants made to the role, of pg_read_all_stats or a role that is a
    # member of it, that do not pass the privileges on.
    grants_without_inherit = (
        """ARRAY(
            SELECT quote_ident(r.rolname)
            FROM pg_auth_members m
            JOIN pg_roles r ON r.oid = m.roleid
            JOIN pg_roles me ON me.oid = m.member
            WHERE me.rolname = current_user
              AND NOT m.inherit_option
              AND pg_has_role(m.roleid, 'pg_read_all_stats', 'MEMBER')
            ORDER BY r.rolname
        )"""
        if version >= GRANT_INHERIT_OPTION_MIN_VERSION
        else "ARRAY[]::text[]"
    )
    return f"""
        SELECT
            current_user AS role,
            quote_ident(current_user) AS role_identifier,
            pg_has_role(current_user, 'pg_read_all_stats', '{privilege}') AS reads_all_stats,
            pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER') AS member,
            (SELECT r.rolinherit FROM pg_roles r WHERE r.rolname = current_user) AS inherits,
            {grants_without_inherit} AS grants_without_inherit
        """


def remedy_for(version: int, cells: dict[str, Any]) -> str | None:
    """The statement that would give the role what the statistics views ask of it, from the row :func:`statistics_access` reads."""
    if cells.get("reads_all_stats"):
        return None
    role = str(cells.get("role_identifier") or cells.get("role") or "")
    if not cells.get("member"):
        return f"GRANT pg_monitor TO {role}"
    if version >= GRANT_INHERIT_OPTION_MIN_VERSION:
        # ALTER ROLE ... INHERIT changes only the grants made after it.
        granted = (cells.get("grants_without_inherit") or ["pg_monitor"])[0]
        return f"GRANT {granted} TO {role} WITH INHERIT TRUE"
    if cells.get("inherits") is False:
        return f"ALTER ROLE {role} INHERIT"
    # It inherits, so a role it is a member through does not: a grant made to
    # it directly passes the privileges on.
    return f"GRANT pg_monitor TO {role}"


async def statistics_access(sql_driver: SqlDriver | SafeSqlDriver) -> StatisticsAccess:
    """Ask the server whether the connecting role may see other roles' statistics.

    The question is the one the server asks before it shows a row: the
    privileges of ``pg_read_all_stats`` from PostgreSQL 15, membership before.
    Both are true for a superuser. Runs in restricted mode.
    """
    version = await get_postgres_version(sql_driver)
    rows = await sql_driver.execute_query(_access_sql(version))
    cells = rows[0].cells if rows else {}
    return StatisticsAccess(
        role=str(cells.get("role") or ""),
        reads_all_stats=bool(cells.get("reads_all_stats")),
        member=bool(cells.get("member")),
        remedy=remedy_for(version, cells),
    )


def is_withheld(row: Mapping[str, Any]) -> bool:
    """Whether PostgreSQL withheld a row's details from the role that read it, as its query text says.

    For a row of ``pg_stat_activity``, ``pg_stat_statements``, or anything
    pg_debug returns from them with their ``query``.
    """
    return row.get("query") == HIDDEN_QUERY_TEXT


async def withheld_backends(sql_driver: SqlDriver | SafeSqlDriver) -> list[dict[str, Any]]:
    """The backends whose details PostgreSQL withholds from the connecting role, and what it still shows of them.

    Returns:
        One dict per backend: ``pid``, ``datname``, ``usename`` (None for the
        server's own processes) and ``application_name``; ``in_transaction``,
        read from its transaction ID, snapshot and the lock on its virtual
        transaction; ``xmin_age``, how many transactions old its snapshot is,
        or None without one; ``locks``, the locks it holds besides those on
        its own transaction, as ``"<mode> on <target>"``; and ``blocking``,
        the pids waiting on it. Empty for a role shown everything. How long a
        backend has been in its transaction or state cannot be read: the
        timestamps are withheld with the rest.
    """
    rows = await sql_driver.execute_query(WITHHELD_BACKENDS_SQL)
    return [row.cells for row in rows] if rows else []


__all__ = [
    "HIDDEN_QUERY_TEXT",
    "IN_TRANSACTION",
    "IN_TRANSACTION_CTE",
    "StatisticsAccess",
    "is_withheld",
    "remedy_for",
    "statistics_access",
    "withheld_backends",
]
