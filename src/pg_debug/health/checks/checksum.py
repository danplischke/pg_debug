"""Pages that failed their checksum when they were read.

With data checksums on, PostgreSQL writes a checksum into every page and
verifies it on each read from disk. A failure means the page changed between
the write and the read, below PostgreSQL: storage, the file system, memory, or
something writing to the data files that should not. From PostgreSQL 12,
``pg_stat_database`` counts the failures per database (and in a row of its
own for the shared catalogs) with the time of the last one.

With checksums off the counters are NULL: a corrupted page is read as it is,
and nothing counts it. That is said, not read as a clean result. Like every
counter, these restart from zero at ``pg_stat_reset()`` and after a crash, so
each finding carries since when it has counted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ...db import get_postgres_version
from ...db.window import database_window
from ..result import HealthCheckResult

CHECKSUM_HEALTH = "Data checksums"

#: ``checksum_failures`` and ``checksum_last_failure`` in pg_stat_database.
CHECKSUM_COUNTERS_MIN_VERSION = 12

SETTING_SQL = "SELECT current_setting('data_checksums') AS data_checksums"

# datid 0 is the row for the shared catalogs, which belong to no database.
FAILURES_SQL = """
SELECT
    d.datname AS database,
    d.datid = 0 AS shared_catalogs,
    d.checksum_failures,
    d.checksum_last_failure,
    d.stats_reset AS database_reset,
    b.stats_reset AS server_reset,
    now() AS now
FROM pg_stat_database d
CROSS JOIN pg_stat_bgwriter b
ORDER BY d.checksum_failures DESC NULLS LAST, d.datname NULLS FIRST
"""


class ChecksumHealthCalc:
    """Reports the checksum failures the server has counted, and whether it can count them."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def checksum_failure_check(self) -> HealthCheckResult:
        """Find databases where a page failed its checksum.

        Each finding names the database (or the shared catalogs, with
        ``kind = "shared catalogs"``), how many failures were counted, when
        the last one was, and the counting window. Checksums being off is
        not a finding, since it is how most servers were created before
        PostgreSQL 18; the summary says that failures cannot be detected.
        """
        version = await get_postgres_version(self.sql_driver)
        setting = await SafeSqlDriver.execute_param_query(self.sql_driver, SETTING_SQL)
        enabled = bool(setting) and str(setting[0].cells.get("data_checksums")) == "on"
        metrics: dict[str, Any] = {"data_checksums": enabled}

        if version < CHECKSUM_COUNTERS_MIN_VERSION:
            on = "on" if enabled else "off"
            return HealthCheckResult(
                name=CHECKSUM_HEALTH,
                healthy=True,
                summary=f"Data checksums are {on}. PostgreSQL {version} does not count checksum failures; they are in the server log only.",
                metrics=metrics,
            )
        if not enabled:
            return HealthCheckResult(
                name=CHECKSUM_HEALTH,
                healthy=True,
                summary=(
                    "Data checksums are off, so PostgreSQL cannot detect a page corrupted below it and counts no failures. "
                    "They can be turned on with pg_checksums while the server is stopped, or at initdb."
                ),
                metrics=metrics,
            )

        rows = [dict(row.cells) for row in await SafeSqlDriver.execute_param_query(self.sql_driver, FAILURES_SQL) or []]
        findings: list[dict[str, Any]] = []
        windows: dict[str, Any] = {}
        for row in rows:
            window = database_window(row.get("database_reset"), row.get("server_reset"), row.get("now") or datetime.now().astimezone())
            windows["(shared catalogs)" if row["shared_catalogs"] else str(row["database"])] = window.to_dict()
            if not row.get("checksum_failures"):
                continue
            findings.append(
                {
                    "kind": "shared catalogs" if row["shared_catalogs"] else "database",
                    "database": None if row["shared_catalogs"] else row["database"],
                    "checksum_failures": int(row["checksum_failures"]),
                    "checksum_last_failure": row["checksum_last_failure"],
                    **window.to_dict(),
                }
            )
        databases = sum(1 for row in rows if not row["shared_catalogs"])
        metrics["databases_examined"] = databases
        metrics["windows"] = windows

        if not findings:
            return HealthCheckResult(
                name=CHECKSUM_HEALTH,
                healthy=True,
                summary=(
                    f"Data checksums are on, and no page has failed its checksum in {databases} {'database' if databases == 1 else 'databases'} "
                    "or the shared catalogs, "
                    "each counted since its last reset or the server's last crash (metrics.windows)."
                ),
                metrics=metrics,
            )

        lines = ["Pages failed their checksum when read, so they changed on disk below PostgreSQL:"]
        for finding in findings:
            subject = "The shared catalogs" if finding["kind"] == "shared catalogs" else f"Database '{finding['database']}'"
            since = f", counting since {finding['counting_since']}" if finding.get("counting_since") else ""
            count = finding["checksum_failures"]
            failures = f"{count:,} {'failure' if count == 1 else 'failures'}"
            lines.append(f"- {subject}: {failures}, the last at {finding['checksum_last_failure']}{since}.")
        lines.append(
            "The server log names each page. Check the storage and memory, and that a backup taken before the first failure exists; "
            "a failure is not cured by a restart."
        )
        return HealthCheckResult(
            name=CHECKSUM_HEALTH,
            healthy=False,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )


__all__ = ["CHECKSUM_HEALTH", "ChecksumHealthCalc"]
