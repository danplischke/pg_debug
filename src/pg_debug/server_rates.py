"""What the server as a whole is doing now: its cumulative counters, measured over an interval.

The statistics views count from their last reset, so a total says what has
happened since then, which may be a month or a minute. Two snapshots a known
time apart say what is happening now: commits, block reads, temporary files,
WAL, checkpoints, I/O by process type, and how fast each replication slot is
holding back WAL.

A counter that starts again from zero between the two snapshots has no
difference to report: what it counted before the reset is gone, and
subtracting would report a negative rate, or one too small. So a group of
counters whose recorded reset moved during the interval is refused, with the
two times, and so is any single counter that went backwards, with the two
values. Refusing is the answer: run it again.

Backends report their counters to the shared statistics when they finish a
transaction, at most about once a second (before PostgreSQL 15, through the
statistics collector, about every half second), so a sample of a few seconds
misses some of what ran at its edges. Ten seconds or more is steadier.

The database's counters count pg-debug's own reads as well. They are read
last in the first snapshot and first in the second, so one of its
transactions, the one that read them, falls inside the interval.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import LiteralString

from .db import SafeSqlDriver
from .db import SqlDriver
from .db import get_postgres_version
from .db.window import checkpoint_window
from .db.window import table_window
from .errors import InvalidArgumentError

logger = logging.getLogger(__name__)

#: Seconds between the two snapshots unless asked otherwise: long enough that
#: what backends had not yet reported at either edge is a small part of it.
DEFAULT_RATES_INTERVAL = 10.0

#: pg_stat_wal came with PostgreSQL 14, pg_stat_io with 16, and 17 moved the
#: checkpoint counters out of pg_stat_bgwriter into pg_stat_checkpointer.
WAL_STATS_MIN_VERSION = 14
IO_STATS_MIN_VERSION = 16
CHECKPOINTER_VIEW_MIN_VERSION = 17
#: pg_replication_slots.wal_status came with PostgreSQL 13.
SLOT_WAL_STATUS_MIN_VERSION = 13

DATABASE_COUNTERS = (
    "xact_commit",
    "xact_rollback",
    "blks_read",
    "blks_hit",
    "tup_returned",
    "tup_fetched",
    "tup_inserted",
    "tup_updated",
    "tup_deleted",
    "temp_files",
    "temp_bytes",
    "deadlocks",
    "conflicts",
)

DATABASE_SQL = """
SELECT clock_timestamp() AS at, datname,
    xact_commit, xact_rollback, blks_read, blks_hit,
    tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
    temp_files, temp_bytes, deadlocks, conflicts
FROM pg_stat_database
WHERE datname = current_database()
"""

WAL_COUNTERS = ("wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full")
WAL_SQL = "SELECT clock_timestamp() AS at, wal_records, wal_fpi, wal_bytes, wal_buffers_full, stats_reset FROM pg_stat_wal"

#: Operations, not bytes: each is one block of ``op_bytes`` (the block size).
#: A counter that does not apply to a kind of I/O is NULL, and its sum too.
IO_COUNTERS = ("reads", "writes", "writebacks", "extends", "hits", "evictions", "reuses", "fsyncs")
IO_SQL = """
SELECT clock_timestamp() AS at, backend_type, object,
    sum(reads) AS reads, sum(writes) AS writes, sum(writebacks) AS writebacks,
    sum(extends) AS extends, sum(hits) AS hits, sum(evictions) AS evictions,
    sum(reuses) AS reuses, sum(fsyncs) AS fsyncs,
    max(stats_reset) AS stats_reset
FROM pg_stat_io
GROUP BY backend_type, object
ORDER BY backend_type, object
"""

CHECKPOINTER_COUNTERS = (
    "checkpoints_timed",
    "checkpoints_requested",
    "checkpoint_write_ms",
    "checkpoint_sync_ms",
    "buffers_checkpoint",
)
BGWRITER_COUNTERS = ("buffers_clean", "maxwritten_clean", "buffers_alloc", "buffers_backend", "buffers_backend_fsync")

CHECKPOINTER_SQL = """
SELECT clock_timestamp() AS at,
    num_timed AS checkpoints_timed, num_requested AS checkpoints_requested,
    write_time AS checkpoint_write_ms, sync_time AS checkpoint_sync_ms,
    buffers_written AS buffers_checkpoint
FROM pg_stat_checkpointer
"""
BGWRITER_SQL = "SELECT clock_timestamp() AS at, buffers_clean, maxwritten_clean, buffers_alloc, stats_reset FROM pg_stat_bgwriter"
#: Before PostgreSQL 17 one view holds both, and the writes backends made
#: themselves, which 17 moved to pg_stat_io.
LEGACY_BGWRITER_SQL = """
SELECT clock_timestamp() AS at,
    checkpoints_timed, checkpoints_req AS checkpoints_requested,
    checkpoint_write_time AS checkpoint_write_ms, checkpoint_sync_time AS checkpoint_sync_ms,
    buffers_checkpoint, buffers_clean, maxwritten_clean, buffers_alloc,
    buffers_backend, buffers_backend_fsync, stats_reset
FROM pg_stat_bgwriter
"""

#: Where WAL is now: written on a primary, replayed on a standby, where
#: pg_current_wal_lsn() raises. CASE evaluates only the branch it takes.
WAL_POSITION = "CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_lsn() END"
POSITION_SQL = f"SELECT clock_timestamp() AS at, pg_is_in_recovery() AS in_recovery, ({WAL_POSITION})::text AS lsn"

SLOT_COUNTERS = ("retained_bytes",)


def _slots_sql(version: int) -> LiteralString:
    wal_status = "wal_status" if version >= SLOT_WAL_STATUS_MIN_VERSION else "NULL::text AS wal_status"
    return f"""
SELECT clock_timestamp() AS at, slot_name, slot_type, active, {wal_status},
    pg_wal_lsn_diff({WAL_POSITION}, restart_lsn)::bigint AS retained_bytes
FROM pg_replication_slots
ORDER BY slot_name
"""


@dataclass(frozen=True)
class CounterRate:
    """One counter over the interval.

    Attributes:
        name: The counter, as the view names it (or as pg-debug names it where
            the views renamed it between versions).
        before: Its value in the first snapshot.
        after: Its value in the second.
        delta: ``after - before``, or None when refused.
        per_second: ``delta`` over the group's measured interval, or None when
            refused.
        refused: Why there is no delta: the counter went backwards, which a
            reset or an overflow does. None when there is one.
    """

    name: str
    before: float | None
    after: float | None
    delta: float | None
    per_second: float | None
    refused: str | None = None


@dataclass(frozen=True)
class RateGroup:
    """The counters of one row of one statistics view, over the interval.

    Attributes:
        measures: What the counters count, the same on every version:
            ``database``, ``wal_position`` (how far WAL was written, or on a
            standby replayed), ``wal``, ``checkpoints``,
            ``background_writer``, ``io`` and ``replication_slot``.
        source: The view they were read from, which differs by version: the
            checkpoint counters are ``pg_stat_checkpointer`` from PostgreSQL
            17 and ``pg_stat_bgwriter`` before it.
        subject: The row: the database, ``backend_type/object`` for I/O, the
            slot's name, ``written`` or ``replayed`` for the WAL position.
            None for a view of one row.
        interval_seconds: Measured between the two snapshots of this view by
            the server's clock, which is what each rate is over. None when
            refused.
        counters: Each counter's delta and rate. Empty when refused.
        refused: Why this group has no rates: the view is not in this
            version, its counters were reset during the interval, or the row
            (a slot) appeared or disappeared during it. None when it has them.
        window: For ``pg_stat_database`` and the checkpoint counters, how long
            they had been counting at the second snapshot
            (:meth:`~pg_debug.db.window.CountingWindow.to_dict`).
        details: What else describes the row: a slot's type, whether it is
            active, its ``wal_status``.
    """

    measures: str
    source: str
    subject: str | None
    interval_seconds: float | None
    counters: list[CounterRate] = field(default_factory=list)
    refused: str | None = None
    window: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def rate(self, name: str) -> float | None:
        """The per-second rate of one counter, or None when it is not here or was refused."""
        for counter in self.counters:
            if counter.name == name:
                return counter.per_second
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServerRates:
    """The server's counters measured over an interval.

    Attributes:
        interval_seconds: The interval asked for. Each group carries the one
            measured between its own two snapshots.
        server_version: The server's major version, which decides which views
            there are.
        groups: One per view row, in the order of :data:`RateGroup.measures`
            above, including the ones refused, with the reason.
        idle_io: How many ``pg_stat_io`` rows did no I/O during the interval
            and are left out of ``groups``.
    """

    interval_seconds: float
    server_version: int
    groups: list[RateGroup]
    idle_io: int = 0

    def group(self, measures: str, subject: str | None = None) -> RateGroup | None:
        """The first group measuring this, or the one of this row when there are several."""
        for group in self.groups:
            if group.measures == measures and (subject is None or group.subject == subject):
                return group
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Reading:
    """One row of one view in one snapshot."""

    source: str
    at: datetime
    values: dict[str, Any]
    #: What says the counters started again: a view's stats_reset, or a
    #: counting window's start. Compared, never interpreted.
    reset: Any = None
    window: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)


#: A snapshot: for each (what it measures, row), its reading.
_Snapshot = dict[tuple[str, str | None], _Reading]


def _number(value: Any) -> float | int | None:
    """A counter as a number: numeric columns come back as Decimal."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def parse_lsn(text: str | None) -> int | None:
    """``16/B374D848`` as the byte position it names."""
    if not text:
        return None
    high, _, low = text.partition("/")
    return (int(high, 16) << 32) + int(low, 16)


def _describe_reset(value: Any) -> str:
    if value is None:
        return "never"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def diff_group(
    measures: str,
    subject: str | None,
    before: _Reading | None,
    after: _Reading | None,
    counters: tuple[str, ...],
    monotonic: bool = True,
) -> RateGroup:
    """The rates of one view row between two snapshots, or the reason there are none.

    Args:
        measures: What the counters count, as :class:`RateGroup` names it.
        subject: The row.
        before: Its reading in the first snapshot, None when it was not there.
        after: Its reading in the second, None when it was not there.
        counters: The counters to compare.
        monotonic: Whether the counters only ever grow until a reset. A
            slot's retained WAL is a level, not a counter: it shrinks when
            the slot's consumer catches up, and a negative change is the
            answer, not a sign of a reset.
    """
    either = after or before
    source = either.source if either else measures
    what = f"{source} ({subject})" if subject else source
    if before is None or after is None:
        when = "appeared" if before is None else "disappeared"
        missing = "first" if before is None else "second"
        return RateGroup(
            measures,
            source,
            subject,
            None,
            refused=f"{what} {when} during the interval, so it has no {missing} snapshot",
            details=either.details if either else {},
        )
    if before.reset != after.reset:
        return RateGroup(
            measures,
            source,
            subject,
            None,
            refused=(
                f"{what} was reset during the interval (counting since {_describe_reset(before.reset)} "
                f"in the first snapshot, since {_describe_reset(after.reset)} in the second), so what it counted before is gone"
            ),
            window=after.window,
            details=after.details,
        )
    seconds = (after.at - before.at).total_seconds()
    if seconds <= 0:
        return RateGroup(
            measures,
            source,
            subject,
            None,
            refused=f"the server's clock read {seconds:g}s between the two snapshots of {what}",
            details=after.details,
        )

    rates: list[CounterRate] = []
    for name in counters:
        first, second = _number(before.values.get(name)), _number(after.values.get(name))
        if first is None and second is None:
            # Not recorded for this row: an I/O operation that does not apply
            # to it, or a counter this version does not have.
            continue
        if first is None or second is None:
            rates.append(CounterRate(name, first, second, None, None, refused=f"{name} was recorded in only one of the two snapshots"))
            continue
        delta = second - first
        if monotonic and delta < 0:
            rates.append(
                CounterRate(
                    name,
                    first,
                    second,
                    None,
                    None,
                    refused=f"{name} went backwards, from {first:,} to {second:,}: it started again from zero during the interval",
                )
            )
            continue
        rates.append(CounterRate(name, first, second, delta, delta / seconds))
    return RateGroup(measures, source, subject, seconds, counters=rates, window=after.window, details=after.details)


class ServerRatesSampler:
    """Takes two snapshots of the server's statistics views and reports the rates between them."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver):
        self.sql_driver = sql_driver

    async def sample(self, interval_seconds: float) -> ServerRates:
        """Measure the server's counters over an interval.

        Args:
            interval_seconds: How long to wait between the two snapshots.

        Returns:
            A :class:`ServerRates`, one group per view row, refused ones
            included with the reason.

        Raises:
            InvalidArgumentError: If the interval is not positive.
        """
        if interval_seconds <= 0:
            raise InvalidArgumentError("The sampling interval must be greater than zero seconds.")
        version = await get_postgres_version(self.sql_driver)
        # This database's counters count pg-debug's own transactions too. Read
        # last in the first snapshot and first in the second, only the one
        # that read them first falls between the two.
        before = await self._snapshot(version)
        before.update(await self._database(last=True))
        await asyncio.sleep(interval_seconds)
        after = await self._database(last=False)
        after.update(await self._snapshot(version))
        groups, idle_io = self._diff(version, before, after)
        return ServerRates(interval_seconds=interval_seconds, server_version=version, groups=groups, idle_io=idle_io)

    @staticmethod
    def _diff(version: int, before: _Snapshot, after: _Snapshot) -> tuple[list[RateGroup], int]:
        groups: list[RateGroup] = []
        idle_io = 0

        def compare(measures: str, counters: tuple[str, ...], monotonic: bool = True) -> list[RateGroup]:
            subjects = [subject for (kind, subject) in before if kind == measures]
            subjects += [subject for (kind, subject) in after if kind == measures and subject not in subjects]
            return [
                diff_group(measures, subject, before.get((measures, subject)), after.get((measures, subject)), counters, monotonic)
                for subject in subjects
            ]

        groups += compare("database", DATABASE_COUNTERS)
        groups += compare("wal_position", ("bytes",))
        if version >= WAL_STATS_MIN_VERSION:
            groups += compare("wal", WAL_COUNTERS)
        else:
            groups.append(
                RateGroup(
                    "wal",
                    "pg_stat_wal",
                    None,
                    None,
                    refused=f"pg_stat_wal came with PostgreSQL {WAL_STATS_MIN_VERSION}; wal_position measures WAL written",
                )
            )
        groups += compare("checkpoints", CHECKPOINTER_COUNTERS)
        groups += compare("background_writer", BGWRITER_COUNTERS)
        if version >= IO_STATS_MIN_VERSION:
            for group in compare("io", IO_COUNTERS):
                if group.refused is None and not any(counter.delta or counter.refused for counter in group.counters):
                    idle_io += 1
                    continue
                groups.append(group)
        else:
            groups.append(RateGroup("io", "pg_stat_io", None, None, refused=f"pg_stat_io came with PostgreSQL {IO_STATS_MIN_VERSION}"))
        for group in compare("replication_slot", SLOT_COUNTERS, monotonic=False):
            if group.refused is None and not group.counters:
                # restart_lsn is NULL: the slot holds back no WAL, so there is
                # nothing to measure, and saying so is the answer.
                group = replace(group, interval_seconds=None, refused=f"slot {group.subject} has reserved no WAL (its restart_lsn is NULL)")
            groups.append(group)
        return groups, idle_io

    async def _rows(self, query: LiteralString) -> list[dict[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query)
        return [dict(row.cells) for row in rows] if rows else []

    async def _database(self, last: bool) -> _Snapshot:
        """This database's counters, with what says when they last started from zero.

        Args:
            last: Whether this is the end of the first snapshot, which reads
                the counters after their window rather than before it, so
                that only the transaction reading them is counted between the
                two. A reset between the two reads is still seen: the window
                read in the second snapshot is the later.
        """
        # The database's own reset, or the server's when a crash discarded
        # every counter: the window says which, and when.
        window = await table_window(self.sql_driver) if last else None
        rows = await self._rows(DATABASE_SQL)
        if window is None:
            window = await table_window(self.sql_driver)
        return {
            ("database", row["datname"]): _Reading("pg_stat_database", row["at"], row, reset=window.since, window=window.to_dict()) for row in rows
        }

    async def _snapshot(self, version: int) -> _Snapshot:
        """Read every view but this database's once. Each reading carries the server's clock when it was read."""
        snapshot: _Snapshot = {}

        for row in await self._rows(POSITION_SQL):
            if row["in_recovery"]:
                reading = _Reading("pg_last_wal_replay_lsn()", row["at"], {"bytes": parse_lsn(row["lsn"])})
                snapshot[("wal_position", "replayed")] = reading
            else:
                snapshot[("wal_position", "written")] = _Reading("pg_current_wal_lsn()", row["at"], {"bytes": parse_lsn(row["lsn"])})

        if version >= WAL_STATS_MIN_VERSION:
            for row in await self._rows(WAL_SQL):
                snapshot[("wal", None)] = _Reading("pg_stat_wal", row["at"], row, reset=row["stats_reset"])

        checkpoints = await checkpoint_window(self.sql_driver)
        if version >= CHECKPOINTER_VIEW_MIN_VERSION:
            for row in await self._rows(CHECKPOINTER_SQL):
                snapshot[("checkpoints", None)] = _Reading(
                    "pg_stat_checkpointer", row["at"], row, reset=checkpoints.since, window=checkpoints.to_dict()
                )
            for row in await self._rows(BGWRITER_SQL):
                snapshot[("background_writer", None)] = _Reading("pg_stat_bgwriter", row["at"], row, reset=row["stats_reset"])
        else:
            for row in await self._rows(LEGACY_BGWRITER_SQL):
                snapshot[("checkpoints", None)] = _Reading("pg_stat_bgwriter", row["at"], row, reset=checkpoints.since, window=checkpoints.to_dict())
                snapshot[("background_writer", None)] = _Reading("pg_stat_bgwriter", row["at"], row, reset=row["stats_reset"])

        if version >= IO_STATS_MIN_VERSION:
            for row in await self._rows(IO_SQL):
                snapshot[("io", f"{row['backend_type']}/{row['object']}")] = _Reading("pg_stat_io", row["at"], row, reset=row["stats_reset"])

        for row in await self._rows(_slots_sql(version)):
            details = {"slot_type": row["slot_type"], "active": row["active"], "wal_status": row["wal_status"]}
            snapshot[("replication_slot", row["slot_name"])] = _Reading("pg_replication_slots", row["at"], row, details=details)

        return snapshot


__all__ = ["DEFAULT_RATES_INTERVAL", "CounterRate", "RateGroup", "ServerRates", "ServerRatesSampler", "diff_group", "parse_lsn"]
