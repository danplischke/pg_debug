"""Multixact wraparound: the second counter vacuum has to freeze.

When more than one transaction locks a row (a foreign key check takes a share
lock on the row it references), the row's xmax holds a multixact ID, which
names a list of members kept in ``pg_multixact``. Multixact IDs wrap around
like transaction IDs, are frozen by vacuum like them, and have their own age
setting, ``autovacuum_multixact_freeze_max_age``. The transaction ID check
says nothing about them.

They have a second limit that age does not show: the member storage, which
runs out at about 4 billion members (about 20 GB on disk) whatever the age of
the oldest multixact. PostgreSQL publishes no member-space figure of its own.
The size of ``pg_multixact/members`` on disk is the nearest evidence, and it
can be read only by a role allowed ``pg_ls_dir`` and ``pg_stat_file``; a
finding argued from it is a candidate. ``pg_stat_slru`` (13 and later) shows
how busy the member storage has been, not how full it is.
"""

from __future__ import annotations

from typing import Any
from typing import LiteralString

import humanize

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ...db import get_postgres_version
from ...severity import Confidence
from ..result import HealthCheckResult

MULTIXACT_HEALTH = "Multixact wraparound"

#: An age at which to start watching, below the default setting of 400
#: million. When member space passes half its capacity PostgreSQL lowers the
#: effective freeze age by itself, and emergency vacuums have been reported
#: to begin near 200 million with the setting at 400 million.
DEFAULT_ALERT_AGE = 200_000_000

#: The same margin the transaction ID check keeps before the hard limit.
DEFAULT_WRAPAROUND_MARGIN = 10_000_000

#: Where PostgreSQL stops assigning multixacts: 2^31 less the 3 million it
#: keeps in reserve (``multiStopLimit`` in multixact.c).
MULTIXACT_STOP_AGE = 2**31 - 3_000_000

#: The member storage's capacity, 2^32 members at 5 bytes each, and the size
#: past which the documentation says every table is vacuumed aggressively more
#: often (about 10 GB).
MEMBER_SPACE_LIMIT_BYTES = 20 * 1024**3
MEMBER_SPACE_AGGRESSIVE_BYTES = 10 * 1024**3

#: The pg_stat_slru row for multixact members appeared in 13, and was renamed in 17.
SLRU_MIN_VERSION = 13
SLRU_RENAMED_VERSION = 17

#: How many of this database's tables to list as the oldest, when none is flagged.
OLDEST_TABLES = 5

SETTINGS_SQL = "SELECT current_setting('autovacuum_multixact_freeze_max_age')::bigint AS freeze_max_age"

DATABASES_SQL = """
SELECT datname AS database, mxid_age(datminmxid)::bigint AS mxid_age, datallowconn AS allows_connections
FROM pg_database
ORDER BY mxid_age(datminmxid) DESC, datname
"""

# A TOAST table is reported as the table it belongs to. A per-table
# autovacuum_multixact_freeze_max_age can only lower the server's setting.
TABLES_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    greatest(mxid_age(c.relminmxid), coalesce(mxid_age(t.relminmxid), 0))::bigint AS mxid_age,
    least(
        current_setting('autovacuum_multixact_freeze_max_age')::bigint,
        coalesce(
            (SELECT split_part(opt, '=', 2)::bigint FROM unnest(c.reloptions) opt WHERE opt LIKE 'autovacuum_multixact_freeze_max_age=%'),
            current_setting('autovacuum_multixact_freeze_max_age')::bigint
        )
    ) AS freeze_max_age
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class t ON t.oid = c.reltoastrelid
WHERE c.relkind IN ('r', 'm')
ORDER BY 3 DESC, 1, 2
LIMIT {}
"""


def slru_sql(version: int) -> LiteralString:
    name = "multixact_member" if version >= SLRU_RENAMED_VERSION else "MultiXactMember"
    return f"""
SELECT name, blks_zeroed, blks_hit, blks_read, blks_written, truncates, stats_reset
FROM pg_stat_slru
WHERE name = '{name}'
"""


#: Whether the role may list and measure files in the data directory. A
#: superuser may; another role needs EXECUTE granted on both.
MEMBER_FILES_ALLOWED_SQL = """
SELECT
    has_function_privilege('pg_ls_dir(text)', 'EXECUTE')
    AND has_function_privilege('pg_stat_file(text)', 'EXECUTE') AS allowed
"""

MEMBER_FILES_SQL = """
SELECT count(*)::bigint AS files, coalesce(sum((pg_stat_file('pg_multixact/members/' || f)).size), 0)::bigint AS bytes
FROM pg_ls_dir('pg_multixact/members') AS f
"""


class MultixactCalc:
    """Reports how close databases and tables are to multixact wraparound, and what can be said of member space."""

    def __init__(
        self,
        sql_driver: SqlDriver | SafeSqlDriver,
        alert_age: int = DEFAULT_ALERT_AGE,
        wraparound_margin: int = DEFAULT_WRAPAROUND_MARGIN,
        max_tables: int = 50,
    ):
        self.sql_driver = sql_driver
        self.alert_age = alert_age
        self.wraparound_margin = wraparound_margin
        self.max_tables = max_tables

    async def multixact_check(self) -> HealthCheckResult:
        """Find databases and tables whose oldest multixact is old enough to matter.

        Each finding has a ``level``: ``wraparound`` within the margin of the
        hard limit; ``past_freeze_max_age`` older than the age at which
        autovacuum should already have forced a freeze, so it is not keeping
        up; ``approaching`` past the alert age (200 million by default). Age
        findings are read from ``pg_database`` and ``pg_class`` and are
        proven. Tables are this database's; another database's tables need a
        connection to it.

        Member space is reported as a ``member_space`` finding, a candidate,
        only when the files could be measured and pass the size at which
        PostgreSQL starts vacuuming every table aggressively. Otherwise the
        summary says what could and could not be read.
        """
        version = await get_postgres_version(self.sql_driver)
        settings = await SafeSqlDriver.execute_param_query(self.sql_driver, SETTINGS_SQL)
        freeze_max_age = int(settings[0].cells["freeze_max_age"]) if settings else 400_000_000
        databases = [dict(row.cells) for row in await SafeSqlDriver.execute_param_query(self.sql_driver, DATABASES_SQL) or []]
        tables = [dict(row.cells) for row in await SafeSqlDriver.execute_param_query(self.sql_driver, TABLES_SQL, [self.max_tables]) or []]

        findings: list[dict[str, Any]] = []
        for database in databases:
            level = self._level(int(database["mxid_age"] or 0), freeze_max_age)
            if level:
                findings.append({**database, "kind": "database", "level": level, "confidence": Confidence.PROVEN.value})
        for table in tables:
            level = self._level(int(table["mxid_age"] or 0), int(table["freeze_max_age"] or freeze_max_age))
            if level:
                findings.append({**table, "kind": "table", "level": level, "confidence": Confidence.PROVEN.value})

        metrics: dict[str, Any] = {
            "autovacuum_multixact_freeze_max_age": freeze_max_age,
            "alert_age": self.alert_age,
            "wraparound_margin": self.wraparound_margin,
            "stop_age": MULTIXACT_STOP_AGE,
            "tables_read_at_most": self.max_tables,
            "oldest_database": databases[0] if databases else None,
            "oldest_tables": tables[:OLDEST_TABLES],
            "member_space_percent": None,
        }
        member_lines = await self._member_space(version, metrics, findings)

        lines: list[str] = []
        if findings:
            lines.append("Multixact IDs old enough to need attention:")
            lines += [_describe(finding, metrics) for finding in findings]
        else:
            oldest = metrics["oldest_database"]
            if oldest:
                lines.append(
                    f"The oldest multixact is {int(oldest['mxid_age'] or 0):,} old, in database '{oldest['database']}': below the alert age "
                    f"of {self.alert_age:,} and autovacuum_multixact_freeze_max_age of {freeze_max_age:,}."
                )
        lines += member_lines

        return HealthCheckResult(
            name=MULTIXACT_HEALTH,
            healthy=not findings,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )

    def _level(self, age: int, freeze_max_age: int) -> str | None:
        if age >= MULTIXACT_STOP_AGE - self.wraparound_margin:
            return "wraparound"
        if age > freeze_max_age:
            return "past_freeze_max_age"
        if age >= self.alert_age:
            return "approaching"
        return None

    async def _member_space(self, version: int, metrics: dict[str, Any], findings: list[dict[str, Any]]) -> list[str]:
        """What can be read of member space: its traffic, and its size on disk where the role may look."""
        lines = ["PostgreSQL publishes no member-space percentage, so none is given."]

        if version >= SLRU_MIN_VERSION:
            rows = await SafeSqlDriver.execute_param_query(self.sql_driver, slru_sql(version))
            metrics["member_slru"] = dict(rows[0].cells) if rows else None
        else:
            metrics["member_slru"] = None

        if isinstance(self.sql_driver, SafeSqlDriver):
            metrics["member_files"] = None
            lines.append("The size of pg_multixact/members was not read: restricted mode does not run pg_ls_dir.")
            return lines

        allowed = await self.sql_driver.execute_query(MEMBER_FILES_ALLOWED_SQL)
        if not (allowed and allowed[0].cells.get("allowed")):
            metrics["member_files"] = None
            lines.append("The size of pg_multixact/members could not be read: the role may not execute pg_ls_dir and pg_stat_file.")
            return lines

        rows = await self.sql_driver.execute_query(MEMBER_FILES_SQL)
        files = dict(rows[0].cells) if rows else {"files": 0, "bytes": 0}
        metrics["member_files"] = files
        size = int(files["bytes"] or 0)
        if size >= MEMBER_SPACE_AGGRESSIVE_BYTES:
            findings.append(
                {
                    "kind": "member_space",
                    "level": "member_space",
                    "bytes": size,
                    "files": int(files["files"] or 0),
                    "aggressive_bytes": MEMBER_SPACE_AGGRESSIVE_BYTES,
                    "limit_bytes": MEMBER_SPACE_LIMIT_BYTES,
                    "confidence": Confidence.CANDIDATE.value,
                }
            )
        else:
            lines.append(
                f"pg_multixact/members takes {humanize.naturalsize(size, binary=True)} on disk, below the "
                f"{humanize.naturalsize(MEMBER_SPACE_AGGRESSIVE_BYTES, binary=True)} past which every table is vacuumed aggressively."
            )
        return lines


def _describe(finding: dict[str, Any], metrics: dict[str, Any]) -> str:
    if finding["kind"] == "member_space":
        return (
            f"- pg_multixact/members takes {humanize.naturalsize(finding['bytes'], binary=True)} on disk, past the "
            f"{humanize.naturalsize(finding['aggressive_bytes'], binary=True)} at which every table is vacuumed aggressively; it runs out "
            f"near {humanize.naturalsize(finding['limit_bytes'], binary=True)} whatever the age. A candidate: the size of the files "
            "stands in for the member space in use, which PostgreSQL does not publish."
        )
    subject = f"Database '{finding['database']}'" if finding["kind"] == "database" else f"Table '{finding['schema']}.{finding['table']}'"
    age = int(finding["mxid_age"] or 0)
    level = finding["level"]
    if level == "wraparound":
        why = f"{MULTIXACT_STOP_AGE - age:,} multixacts from the point where PostgreSQL stops assigning them"
    elif level == "past_freeze_max_age":
        limit = int(finding.get("freeze_max_age") or metrics["autovacuum_multixact_freeze_max_age"])
        why = f"past autovacuum_multixact_freeze_max_age of {limit:,}, so autovacuum should already be forcing a freeze and is not keeping up"
    else:
        why = f"past the alert age of {metrics['alert_age']:,}"
    return f"- {subject} has a multixact age of {age:,}, {why}"


__all__ = ["DEFAULT_ALERT_AGE", "MULTIXACT_HEALTH", "MULTIXACT_STOP_AGE", "MultixactCalc", "slru_sql"]
