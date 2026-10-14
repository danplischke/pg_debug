"""Reviewing the server's settings against what it is actually doing.

There is no shortage of tools that take the machine's memory and hand back a
postgresql.conf. This is not one of them: every finding here is argued from a
measurement taken on this server, and a setting whose default is doing no harm
is left alone. ``shared_buffers`` well below the size of the database is only
worth mentioning if the cache is actually missing; ``work_mem`` is only worth
raising if queries are actually spilling to disk. The counters that say so are
already being kept.

What this deliberately does not do is guess at hardware. PostgreSQL cannot see
how much memory the machine has or whether its storage is solid state, and
neither can a client connected over the network, so no finding here rests on
assuming either.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from .db import SafeSqlDriver
from .db import SqlDriver
from .db.extensions import get_postgres_version
from .platform import Platform
from .platform import PlatformDetection
from .severity import SEVERITY_ORDER
from .severity import Severity

logger = logging.getLogger(__name__)

#: Below this, a cache miss rate says more about a cold server than about
#: shared_buffers.
MIN_BLOCK_READS = 100_000

#: A hit rate above this means the cache is doing its job, whatever size it is.
GOOD_CACHE_HIT_RATE = 0.99

#: Queries spilling less than this to disk are not worth reconfiguring for.
MIN_TEMP_BYTES = 100 * 1024 * 1024

#: Below this many checkpoints the ratio between timed and requested is noise.
MIN_CHECKPOINTS = 10

#: The settings worth looking at. Everything else is left alone.
_SETTINGS = (
    "shared_buffers",
    "work_mem",
    "maintenance_work_mem",
    "effective_cache_size",
    "max_wal_size",
    "random_page_cost",
    "fsync",
    "full_page_writes",
    "autovacuum",
    "log_min_duration_statement",
    "idle_in_transaction_session_timeout",
    "max_connections",
)

SETTINGS_SQL = """
SELECT name, setting, unit, source, boot_val, pending_restart
FROM pg_settings
WHERE name = ANY({}::text[])
"""


@dataclass
class ConfigFinding:
    """One setting worth changing, and the measurement that says so.

    Attributes:
        setting: The name in ``pg_settings``.
        current: Its value now, in the units it is written in.
        recommended: What to set it to, or ``None`` where the right value
            depends on something this tool cannot see.
        severity: How much it matters.
        summary: One sentence.
        detail: The argument, in terms of this server's own numbers.
        evidence: The measurements behind it.
    """

    setting: str
    current: str
    severity: Severity
    summary: str
    detail: str
    recommended: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the finding as a JSON-friendly dict."""
        result: dict[str, Any] = {
            "setting": self.setting,
            "current": self.current,
            "severity": self.severity.value,
            "summary": self.summary,
            "detail": self.detail,
            "evidence": self.evidence,
        }
        if self.recommended is not None:
            result["recommended"] = self.recommended
        return result


class ConfigurationReviewer:
    """Reads pg_settings, and the counters that say whether they are working."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver, platform: PlatformDetection | None = None):
        """
        Args:
            sql_driver: The driver to read the settings and counters through.
            platform: The managed platform the server runs on, which decides
                whether ``fsync = off`` is a risk. Left out, it is the risk it
                is on PostgreSQL itself.
        """
        self.sql_driver = sql_driver
        self.platform = platform

    async def review(self) -> list[ConfigFinding]:
        """Review the server's configuration.

        Returns:
            :class:`ConfigFinding` objects, most severe first. An empty list
            means nothing measurable is wrong with the configuration — not that
            it is optimal, which is not a thing this can know.
        """
        settings = await self._settings()
        measurements = await self._measurements()

        # The durability rule is the one whose verdict depends on the platform.
        verdicts = [_durability(settings, measurements, self.platform)]
        verdicts += [rule(settings, measurements) for rule in _RULES]
        findings = [finding for finding in verdicts if finding is not None]

        return sorted(findings, key=lambda finding: SEVERITY_ORDER[finding.severity])

    async def _settings(self) -> dict[str, dict[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, SETTINGS_SQL, [list(_SETTINGS)])
        return {row.cells["name"]: dict(row.cells) for row in rows or []}

    async def _measurements(self) -> dict[str, Any]:
        """The counters the rules argue from."""
        version = await get_postgres_version(self.sql_driver)
        # PostgreSQL 17 moved the checkpoint counters out of pg_stat_bgwriter
        # into pg_stat_checkpointer, and renamed them.
        if version >= 17:
            checkpoints = "SELECT num_timed AS timed, num_requested AS requested FROM pg_stat_checkpointer"
        else:
            checkpoints = "SELECT checkpoints_timed AS timed, checkpoints_req AS requested FROM pg_stat_bgwriter"

        rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            f"""
            SELECT
                d.blks_read,
                d.blks_hit,
                d.temp_files,
                d.temp_bytes,
                d.xact_commit + d.xact_rollback AS transactions,
                pg_database_size(current_database()) AS database_bytes,
                c.timed AS checkpoints_timed,
                c.requested AS checkpoints_requested
            FROM pg_stat_database d
            CROSS JOIN ({checkpoints}) c
            WHERE d.datname = current_database()
            """,
        )
        return dict(rows[0].cells) if rows else {}


# -- Units ------------------------------------------------------------------


def _as_bytes(setting: dict[str, Any] | None) -> int | None:
    """A memory setting in bytes, whatever unit pg_settings reports it in."""
    if not setting:
        return None
    try:
        value = float(setting["setting"])
    except (TypeError, ValueError):
        return None
    multipliers = {None: 1, "": 1, "B": 1, "kB": 1024, "MB": 1024**2, "GB": 1024**3, "8kB": 8 * 1024, "16kB": 16 * 1024}
    unit = setting.get("unit")
    if unit not in multipliers:
        return None
    return int(value * multipliers[unit])


def _humanize(size: int | None) -> str:
    if size is None:
        return "unknown"
    remaining = float(size)
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if remaining < 1024 or unit == "TB":
            return f"{remaining:.0f} {unit}" if unit == "bytes" else f"{remaining:.1f} {unit}"
        remaining /= 1024.0
    return f"{size} bytes"


def _is_default(setting: dict[str, Any] | None) -> bool:
    return bool(setting) and setting.get("source") == "default"


def _off(setting: dict[str, Any] | None) -> bool:
    return bool(setting) and str(setting.get("setting")).lower() in ("off", "false", "0")


Rule = Callable[[dict[str, dict[str, Any]], dict[str, Any]], ConfigFinding | None]


# -- Rules ------------------------------------------------------------------


def _durability(
    settings: dict[str, dict[str, Any]],
    _measurements: dict[str, Any],
    platform: PlatformDetection | None = None,
) -> ConfigFinding | None:
    """fsync and full_page_writes exist to survive a crash.

    Except fsync on Neon, which runs every compute with it off: a commit there
    is acknowledged once a quorum of safekeepers holds its WAL, so the
    compute's own fsync is not what makes it durable. That is reported as a
    note, low enough not to fail ``--exit-code``, rather than left out, so
    that the reader sees the setting and the signal the platform was
    recognised by, and can tell if the recognition was wrong. A full_page_writes
    that is off is still reported in its place, whatever the platform.
    """
    neon = platform if platform is not None and platform.platform is Platform.NEON else None
    for name, consequence in (
        ("fsync", "a crash or power loss can leave the database unrecoverably corrupt, not merely missing recent commits"),
        ("full_page_writes", "a crash during a write can tear a page in half, and recovery has nothing to repair it from"),
    ):
        setting = settings.get(name)
        if _off(setting) and not (neon is not None and name == "fsync"):
            return ConfigFinding(
                setting=name,
                current="off",
                recommended="on",
                severity=Severity.HIGH,
                summary=f"{name} is off, so this server cannot be trusted to survive a crash.",
                detail=(
                    f"With {name} off, {consequence}. This is only a defensible setting for a database whose "
                    f"contents can be thrown away and rebuilt — a test fixture, or a replica that would be "
                    f"recreated from scratch."
                ),
                evidence={"source": setting.get("source") if setting else None},
            )

    fsync = settings.get("fsync")
    if neon is not None and _off(fsync):
        return ConfigFinding(
            setting="fsync",
            current="off",
            severity=Severity.LOW,
            summary="fsync is off, as Neon runs every compute; that does not put commits at risk here.",
            detail=(
                "Neon acknowledges a commit once a quorum of its safekeepers holds the commit's WAL, so the compute's "
                "own fsync is not what makes a commit durable, and Neon fixes the setting: there is nothing to change. "
                f"pg-debug took this server for Neon because {neon.signal}. If that is wrong, this is the most "
                "serious finding there is: with fsync off, a crash or power loss can leave the database unrecoverably "
                "corrupt."
            ),
            evidence={"source": fsync.get("source") if fsync else None, "platform": neon.platform.value, "platform_signal": neon.signal},
        )
    return None


def _autovacuum_off(settings: dict[str, dict[str, Any]], _measurements: dict[str, Any]) -> ConfigFinding | None:
    setting = settings.get("autovacuum")
    if not _off(setting):
        return None
    return ConfigFinding(
        setting="autovacuum",
        current="off",
        recommended="on",
        severity=Severity.HIGH,
        summary="Autovacuum is switched off for the whole server.",
        detail=(
            "Nothing reclaims dead rows and nothing freezes old transaction ids. The table will bloat without "
            "limit, and the server will eventually shut down to avoid transaction id wraparound. Turning it off "
            "to reduce load postpones a much larger amount of the same work."
        ),
        evidence={"source": setting.get("source") if setting else None},
    )


def _work_mem(settings: dict[str, dict[str, Any]], measurements: dict[str, Any]) -> ConfigFinding | None:
    """Only worth raising if queries are actually spilling to disk."""
    temp_bytes = int(measurements.get("temp_bytes") or 0)
    temp_files = int(measurements.get("temp_files") or 0)
    if temp_bytes < MIN_TEMP_BYTES or not temp_files:
        return None

    setting = settings.get("work_mem")
    current = _as_bytes(setting)
    average = temp_bytes // temp_files
    return ConfigFinding(
        setting="work_mem",
        current=_humanize(current),
        recommended=_humanize(max((current or 0) * 2, average)),
        severity=Severity.MEDIUM,
        summary=f"Queries have spilled {_humanize(temp_bytes)} to disk in {temp_files:,} temporary files.",
        detail=(
            f"A sort or hash that does not fit in work_mem is written to disk instead. The average spill here is "
            f"{_humanize(average)}, against a work_mem of {_humanize(current)}. Raise it carefully: the limit "
            f"applies per operation, not per query, so a complex query with several sorts can use it several "
            f"times over, and every connection can do so at once."
        ),
        evidence={"temp_files": temp_files, "temp_bytes": temp_bytes, "average_spill_bytes": average},
    )


def _checkpoints(settings: dict[str, dict[str, Any]], measurements: dict[str, Any]) -> ConfigFinding | None:
    """Requested checkpoints usually mean WAL filled up before the timer.

    Usually, not always: an explicit CHECKPOINT, a base backup and a shutdown
    are all counted as requested, which is why the finding says so rather than
    asserting write volume.
    """
    timed = int(measurements.get("checkpoints_timed") or 0)
    requested = int(measurements.get("checkpoints_requested") or 0)
    if timed + requested < MIN_CHECKPOINTS or requested <= timed:
        return None

    setting = settings.get("max_wal_size")
    current = _as_bytes(setting)
    share = requested / (timed + requested)
    return ConfigFinding(
        setting="max_wal_size",
        current=_humanize(current),
        recommended=_humanize((current or 0) * 2),
        severity=Severity.MEDIUM,
        summary=f"{share:.0%} of checkpoints happened on request rather than on schedule.",
        detail=(
            f"{requested:,} checkpoints were requested against {timed:,} that came round on the timer. The usual "
            f"reason is max_wal_size filling up before checkpoint_timeout elapses, and those arrive at whatever "
            f"moment the writes do — normally the worst one — each writing out more full-page images than a "
            f"spaced-out checkpoint would. Raising max_wal_size trades disk space for smoother writes. Note that "
            f"an explicit CHECKPOINT command, a backup and a shutdown are counted here too, so check this is "
            f"write volume before acting on it."
        ),
        evidence={"checkpoints_timed": timed, "checkpoints_requested": requested},
    )


def _shared_buffers(settings: dict[str, dict[str, Any]], measurements: dict[str, Any]) -> ConfigFinding | None:
    """Small shared_buffers is only a problem when the cache is missing."""
    reads = int(measurements.get("blks_read") or 0)
    hits = int(measurements.get("blks_hit") or 0)
    if reads + hits == 0 or reads < MIN_BLOCK_READS:
        return None

    hit_rate = hits / (reads + hits)
    if hit_rate >= GOOD_CACHE_HIT_RATE:
        return None

    setting = settings.get("shared_buffers")
    current = _as_bytes(setting)
    database_bytes = int(measurements.get("database_bytes") or 0)
    return ConfigFinding(
        setting="shared_buffers",
        current=_humanize(current),
        recommended=None,
        severity=Severity.MEDIUM,
        summary=f"The buffer cache is serving {hit_rate:.1%} of reads, against a database of {_humanize(database_bytes)}.",
        detail=(
            f"shared_buffers is {_humanize(current)} and {reads:,} blocks have been read from disk. A quarter of "
            f"the machine's memory is the usual starting point, but this tool cannot see how much memory the "
            f"machine has, so no figure is suggested — measure the hit rate again after changing it."
        ),
        evidence={
            "cache_hit_rate": round(hit_rate, 4),
            "blocks_read": reads,
            "blocks_hit": hits,
            "database_bytes": database_bytes,
        },
    )


def _effective_cache_size(settings: dict[str, dict[str, Any]], measurements: dict[str, Any]) -> ConfigFinding | None:
    setting = settings.get("effective_cache_size")
    if not _is_default(setting):
        return None
    current = _as_bytes(setting)
    database_bytes = int(measurements.get("database_bytes") or 0)
    if not current or database_bytes < current:
        return None

    return ConfigFinding(
        setting="effective_cache_size",
        current=_humanize(current),
        recommended=None,
        severity=Severity.LOW,
        summary="effective_cache_size is at its default, and the database is larger than it.",
        detail=(
            f"It reserves no memory; it tells the planner how much of the database it can expect to find cached, "
            f"between shared_buffers and the operating system. Left at the {_humanize(current)} default while the "
            f"database is {_humanize(database_bytes)}, the planner assumes less caching than it will get and "
            f"leans towards sequential scans. Half to three quarters of the machine's memory is usual."
        ),
        evidence={"database_bytes": database_bytes, "source": setting.get("source") if setting else None},
    )


def _random_page_cost(settings: dict[str, dict[str, Any]], measurements: dict[str, Any]) -> ConfigFinding | None:
    """The default assumes a spinning disk, and says so in the documentation."""
    setting = settings.get("random_page_cost")
    if not _is_default(setting):
        return None
    reads = int(measurements.get("blks_read") or 0)
    hits = int(measurements.get("blks_hit") or 0)
    if reads + hits < MIN_BLOCK_READS:
        return None
    hit_rate = hits / (reads + hits)
    if hit_rate < GOOD_CACHE_HIT_RATE:
        return None

    return ConfigFinding(
        setting="random_page_cost",
        current="4.0",
        recommended=None,
        severity=Severity.LOW,
        summary=f"random_page_cost is at the default of 4.0 while {hit_rate:.1%} of reads are served from cache.",
        detail=(
            "The 4.0 default is calibrated for a random read from a rotating disk being four times the cost of a "
            "sequential one. Reads that come from memory cost the same either way, and this server is serving "
            "almost all of them from memory, so 4.0 overstates the cost of an index scan and the planner will "
            "prefer sequential scans more often than it should. 1.1 is usual for solid state storage. This tool "
            "cannot see what your storage is, so change it deliberately and check plans afterwards, rather than "
            "on the strength of this finding alone."
        ),
        evidence={"cache_hit_rate": round(hit_rate, 4), "blocks_read": reads, "blocks_hit": hits},
    )


def _slow_query_log(settings: dict[str, dict[str, Any]], _measurements: dict[str, Any]) -> ConfigFinding | None:
    setting = settings.get("log_min_duration_statement")
    if not setting or str(setting.get("setting")) != "-1":
        return None
    return ConfigFinding(
        setting="log_min_duration_statement",
        current="-1 (nothing logged)",
        recommended="1000",
        severity=Severity.LOW,
        summary="No statement is ever logged for being slow.",
        detail=(
            "pg_stat_statements says which query shapes are expensive on average; the log says which individual "
            "execution was slow, with its parameters and the moment it happened. Those are different questions, "
            "and the second one is the one asked during an incident. A threshold of a second or two logs very "
            "little on a healthy server."
        ),
        evidence={"source": setting.get("source")},
    )


def _idle_transaction_timeout(settings: dict[str, dict[str, Any]], _measurements: dict[str, Any]) -> ConfigFinding | None:
    setting = settings.get("idle_in_transaction_session_timeout")
    if not setting or str(setting.get("setting")) != "0":
        return None
    return ConfigFinding(
        setting="idle_in_transaction_session_timeout",
        current="0 (no limit)",
        recommended="60000",
        severity=Severity.LOW,
        summary="A transaction left open by a client can stay open indefinitely.",
        detail=(
            "An open transaction that has written, or that reads at REPEATABLE READ, holds back the point beyond "
            "which VACUUM can reclaim anything — for the whole database, not just its own tables. A client that "
            "crashes mid-transaction, or a connection pool that hands back a session without rolling it back, "
            "then produces bloat that no amount of vacuuming clears. See the 'Vacuum horizon' health check for "
            "whether that is happening now."
        ),
        evidence={"source": setting.get("source")},
    )


#: The rules that read only the settings and the counters. _durability is not
#: among them: the reviewer calls it with the platform too.
_RULES: list[Rule] = [
    _autovacuum_off,
    _work_mem,
    _checkpoints,
    _shared_buffers,
    _effective_cache_size,
    _random_page_cost,
    _slow_query_log,
    _idle_transaction_timeout,
]


__all__ = [
    "GOOD_CACHE_HIT_RATE",
    "MIN_BLOCK_READS",
    "MIN_CHECKPOINTS",
    "MIN_TEMP_BYTES",
    "ConfigFinding",
    "ConfigurationReviewer",
]
