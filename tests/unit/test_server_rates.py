"""Server-level counters measured over an interval, and the resets that refuse a delta."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import InvalidArgumentError
from pg_debug.server_rates import BGWRITER_SQL
from pg_debug.server_rates import CHECKPOINTER_SQL
from pg_debug.server_rates import DATABASE_SQL
from pg_debug.server_rates import IO_SQL
from pg_debug.server_rates import LEGACY_BGWRITER_SQL
from pg_debug.server_rates import POSITION_SQL
from pg_debug.server_rates import WAL_SQL
from pg_debug.server_rates import ServerRatesSampler
from pg_debug.server_rates import _Reading
from pg_debug.server_rates import _slots_sql
from pg_debug.server_rates import diff_group
from pg_debug.server_rates import parse_lsn

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
RESET = datetime(2026, 9, 1, tzinfo=UTC)


def reading(seconds: float, reset: Any = RESET, source: str = "pg_stat_wal", **values: Any) -> _Reading:
    return _Reading(source, T0 + timedelta(seconds=seconds), values, reset=reset)


# -- One view row between two snapshots ---------------------------------------


def test_a_counter_is_reported_as_a_rate_over_the_measured_interval():
    group = diff_group("wal", None, reading(0, wal_records=100), reading(4, wal_records=500), ("wal_records",))

    assert group.interval_seconds == 4
    (counter,) = group.counters
    assert (counter.delta, counter.per_second, counter.refused) == (400, 100.0, None)


def test_numeric_counters_come_back_as_numbers():
    group = diff_group("wal", None, reading(0, wal_bytes=Decimal("1000")), reading(2, wal_bytes=Decimal("3000")), ("wal_bytes",))

    assert group.rate("wal_bytes") == 1000.0
    assert isinstance(group.counters[0].delta, int)


def test_a_reset_during_the_interval_refuses_the_whole_view():
    """The reported failure this guards against: a reset between the snapshots reads as a negative rate, or as a tiny one."""
    group = diff_group(
        "wal",
        None,
        reading(0, wal_records=10_000),
        reading(2, reset=T0 + timedelta(seconds=1), wal_records=50),
        ("wal_records",),
    )

    assert group.counters == []
    assert group.interval_seconds is None
    assert group.refused is not None
    assert "reset during the interval" in group.refused
    assert RESET.isoformat() in group.refused


def test_a_reset_with_no_reset_time_before_it_is_seen():
    """pg_stat_database.stats_reset is NULL until the first pg_stat_reset()."""
    group = diff_group("database", "app", reading(0, reset=None, xact_commit=5), reading(2, xact_commit=1), ("xact_commit",))

    assert group.refused is not None
    assert "counting since never" in group.refused


def test_a_counter_that_went_backwards_is_refused_with_both_values():
    """No recorded reset moved, and yet the counter is lower: not a rate."""
    group = diff_group(
        "database", "app", reading(0, xact_commit=900, deadlocks=1), reading(2, xact_commit=40, deadlocks=1), ("xact_commit", "deadlocks")
    )

    commits, deadlocks = group.counters
    assert commits.delta is None and commits.per_second is None
    assert commits.refused is not None and "from 900 to 40" in commits.refused
    assert deadlocks.delta == 0 and deadlocks.refused is None
    assert group.refused is None


def test_a_level_that_falls_is_a_rate_and_not_a_reset():
    """A slot's retained WAL shrinks when its consumer catches up."""
    group = diff_group(
        "replication_slot",
        "standby",
        reading(0, source="pg_replication_slots", reset=None, retained_bytes=4096),
        reading(2, source="pg_replication_slots", reset=None, retained_bytes=0),
        ("retained_bytes",),
        monotonic=False,
    )

    assert group.rate("retained_bytes") == -2048.0


def test_a_row_that_appears_or_disappears_during_the_interval_is_refused():
    appeared = diff_group("replication_slot", "new", None, reading(1, source="pg_replication_slots"), ("retained_bytes",))
    gone = diff_group("replication_slot", "old", reading(1, source="pg_replication_slots"), None, ("retained_bytes",))

    assert appeared.refused is not None and "appeared during the interval" in appeared.refused
    assert gone.refused is not None and "disappeared during the interval" in gone.refused


def test_a_counter_the_row_does_not_record_is_left_out():
    """pg_stat_io has NULL for an operation that does not apply, such as extends on a bulk read."""
    group = diff_group("io", "client backend/relation", reading(0, reads=1, extends=None), reading(1, reads=3, extends=None), ("reads", "extends"))

    assert [counter.name for counter in group.counters] == ["reads"]


def test_lsns_are_byte_positions():
    assert parse_lsn("0/0") == 0
    assert parse_lsn("16/B374D848") == (0x16 << 32) + 0xB374D848
    assert parse_lsn(None) is None


# -- The sampler ----------------------------------------------------------------


class FakeServer:
    """Answers each of the sampler's statements from the snapshot it is in."""

    def __init__(self, version: int, first: dict[str, list[dict[str, Any]]], second: dict[str, list[dict[str, Any]]]):
        self.version = version
        self.snapshots = [first, second]
        self.phase = 0
        self.queries: list[str] = []
        self.driver = MagicMock(spec=SqlDriver)
        self.driver.execute_query = AsyncMock(side_effect=self.answer)

    async def answer(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        at = T0 + timedelta(seconds=10 * self.phase)
        for marker, rows in self.snapshots[self.phase].items():
            if marker in query:
                return [SqlDriver.RowResult(cells={"at": at, **row}) for row in rows]
        return []

    async def sleep(self, seconds: float) -> None:
        self.phase = 1


def snapshot(commits: int, *, wal_reset=RESET, slots=(("standby", 1000),), io_reads=10, checkpoints=3, in_recovery=False):
    return {
        "pg_stat_database d": [{"database_reset": RESET, "server_reset": RESET, "now": T0}],
        "FROM pg_stat_database\n": [{"datname": "app", "xact_commit": commits, "xact_rollback": 0}],
        "pg_is_in_recovery() AS in_recovery": [{"in_recovery": in_recovery, "lsn": f"0/{commits * 16:X}"}],
        "FROM pg_stat_wal": [
            {"wal_records": commits, "wal_fpi": 0, "wal_bytes": Decimal(commits * 100), "wal_buffers_full": 0, "stats_reset": wal_reset}
        ],
        "AS server_reset, now() AS now FROM pg_stat_checkpointer": [{"server_reset": RESET, "now": T0}],
        "AS server_reset, now() AS now FROM pg_stat_bgwriter": [{"server_reset": RESET, "now": T0}],
        "FROM pg_stat_checkpointer\n": [{"checkpoints_timed": checkpoints, "checkpoints_requested": 0}],
        "maxwritten_clean, buffers_alloc, stats_reset FROM pg_stat_bgwriter": [{"buffers_clean": 1, "stats_reset": RESET}],
        "checkpoints_req AS checkpoints_requested": [
            {"checkpoints_timed": checkpoints, "checkpoints_requested": 0, "buffers_clean": 1, "stats_reset": RESET}
        ],
        "FROM pg_stat_io": [
            {"backend_type": "client backend", "object": "relation", "reads": io_reads, "hits": 5, "stats_reset": RESET},
            {"backend_type": "checkpointer", "object": "relation", "reads": None, "writes": 0, "stats_reset": RESET},
        ],
        "FROM pg_replication_slots": [
            {"slot_name": name, "slot_type": "physical", "active": True, "wal_status": "reserved", "retained_bytes": retained}
            for name, retained in slots
        ],
    }


async def sample(server: FakeServer, interval: float = 10):
    with (
        patch("pg_debug.server_rates.get_postgres_version", AsyncMock(return_value=server.version)),
        patch("pg_debug.db.window.get_postgres_version", AsyncMock(return_value=server.version)),
        patch("pg_debug.server_rates.asyncio.sleep", AsyncMock(side_effect=server.sleep)),
    ):
        return await ServerRatesSampler(server.driver).sample(interval)


@pytest.mark.asyncio
async def test_every_view_is_measured_on_16():
    server = FakeServer(16, snapshot(100, io_reads=10), snapshot(150, io_reads=30, slots=(("standby", 5000),)))

    rates = await sample(server)

    assert rates.server_version == 16
    database = rates.group("database", "app")
    assert database is not None and database.rate("xact_commit") == 5.0
    assert database.window is not None and database.window["counting_began_with"] == "reset"
    position = rates.group("wal_position", "written")
    assert position is not None and position.rate("bytes") == 50 * 16 / 10
    wal = rates.group("wal")
    assert wal is not None and wal.rate("wal_bytes") == 500.0
    checkpoints = rates.group("checkpoints")
    assert checkpoints is not None and checkpoints.source == "pg_stat_bgwriter"
    io = rates.group("io", "client backend/relation")
    assert io is not None and io.rate("reads") == 2.0
    # The checkpointer did no I/O: counted, not listed.
    assert rates.group("io", "checkpointer/relation") is None
    assert rates.idle_io == 1
    slot = rates.group("replication_slot", "standby")
    assert slot is not None and slot.rate("retained_bytes") == 400.0
    assert slot.counters[0].after == 5000
    assert slot.details == {"slot_type": "physical", "active": True, "wal_status": "reserved"}


@pytest.mark.asyncio
async def test_this_database_is_read_last_in_the_first_snapshot_and_first_in_the_second():
    """So that only one of pg-debug's own transactions falls between its two reads."""
    server = FakeServer(16, snapshot(1), snapshot(2))

    await sample(server)

    first, second = [i for i, query in enumerate(server.queries) if query == DATABASE_SQL]
    assert second == first + 1
    # The window that says whether they were reset is read on the far side of each.
    assert "pg_stat_database d" in server.queries[first - 1]
    assert "pg_stat_database d" in server.queries[second + 1]


@pytest.mark.asyncio
async def test_a_reset_of_one_view_refuses_that_view_alone():
    server = FakeServer(16, snapshot(100), snapshot(150, wal_reset=T0 + timedelta(seconds=5)))

    rates = await sample(server)

    wal = rates.group("wal")
    assert wal is not None and wal.refused is not None and "reset during the interval" in wal.refused
    database = rates.group("database", "app")
    assert database is not None and database.refused is None


@pytest.mark.asyncio
async def test_seventeen_reads_the_checkpointer_from_its_own_view():
    server = FakeServer(17, snapshot(1, checkpoints=3), snapshot(2, checkpoints=4))

    rates = await sample(server)

    checkpoints = rates.group("checkpoints")
    assert checkpoints is not None and checkpoints.source == "pg_stat_checkpointer"
    assert checkpoints.rate("checkpoints_timed") == 0.1
    assert LEGACY_BGWRITER_SQL not in server.queries
    assert not any("checkpoints_req AS" in query for query in server.queries)


@pytest.mark.asyncio
async def test_views_the_version_lacks_are_refused_with_the_version():
    server = FakeServer(13, snapshot(1), snapshot(2))

    rates = await sample(server)

    wal, io = rates.group("wal"), rates.group("io")
    assert wal is not None and wal.refused is not None and "PostgreSQL 14" in wal.refused
    assert io is not None and io.refused is not None and "PostgreSQL 16" in io.refused
    assert not any("pg_stat_wal" in query or "pg_stat_io" in query for query in server.queries)


@pytest.mark.asyncio
async def test_a_standby_measures_wal_replayed():
    server = FakeServer(16, snapshot(1, in_recovery=True), snapshot(2, in_recovery=True))

    rates = await sample(server)

    position = rates.group("wal_position", "replayed")
    assert position is not None and position.source == "pg_last_wal_replay_lsn()"
    assert rates.group("wal_position", "written") is None


@pytest.mark.asyncio
async def test_a_slot_with_no_reserved_wal_says_so():
    server = FakeServer(16, snapshot(1, slots=(("lazy", None),)), snapshot(2, slots=(("lazy", None),)))

    rates = await sample(server)

    slot = rates.group("replication_slot", "lazy")
    assert slot is not None and slot.refused is not None and "reserved no WAL" in slot.refused


@pytest.mark.asyncio
async def test_a_non_positive_interval_is_refused():
    with pytest.raises(InvalidArgumentError, match="greater than zero"):
        await ServerRatesSampler(MagicMock(spec=SqlDriver)).sample(0)


@pytest.mark.parametrize("version", [12, 13, 16])
def test_every_statement_runs_in_restricted_mode(version):
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    for sql in (DATABASE_SQL, POSITION_SQL, WAL_SQL, IO_SQL, CHECKPOINTER_SQL, BGWRITER_SQL, LEGACY_BGWRITER_SQL, _slots_sql(version)):
        safe._validate(sql)
