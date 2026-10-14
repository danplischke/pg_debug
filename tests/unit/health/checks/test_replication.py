"""Replication health: what a replica and a primary each report, and what is said when it cannot be read."""

from typing import Any

import pytest

from pg_debug.db import SqlDriver
from pg_debug.health.checks.replication import ReplicationCalc

STREAMING = {"status": "streaming", "sender_host": "primary.internal", "latest_end_time": "2026-10-14 12:00:00+00"}


class FakeDriver:
    """Answers each query the check makes from the server it describes."""

    def __init__(
        self,
        replica: bool,
        receiver: dict[str, Any] | None = None,
        standbys: tuple[str, ...] = (),
        lag_error: str | None = None,
    ):
        self.replica = replica
        self.receiver = receiver
        self.standbys = standbys
        self.lag_error = lag_error
        self.queries: list[str] = []

    async def execute_query(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        if "server_version_num" in query:
            return [SqlDriver.RowResult(cells={"server_version_num": "160000"})]
        if "AS replication_lag" in query:
            if self.lag_error:
                raise RuntimeError(self.lag_error)
            return [SqlDriver.RowResult(cells={"replication_lag": 0})]
        if "pg_is_in_recovery()" in query:
            return [SqlDriver.RowResult(cells={"pg_is_in_recovery": self.replica})]
        if "pg_stat_wal_receiver" in query:
            return [SqlDriver.RowResult(cells=self.receiver)] if self.receiver is not None else []
        if "pg_stat_replication" in query:
            names = [name for name in self.standbys if "walproposer" not in query or name != "walproposer"]
            return [SqlDriver.RowResult(cells={"state": "streaming"}) for _ in names]
        return []


async def check(driver: FakeDriver):
    return await ReplicationCalc(driver).replication_health_check()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_streaming_replica_with_no_standbys_of_its_own_is_healthy():
    """Its pg_stat_replication is empty, because nothing streams from it; that is not a fault."""
    result = await check(FakeDriver(replica=True, receiver=STREAMING))

    assert result.healthy
    assert "actively replicating from primary primary.internal" in result.summary
    assert result.metrics["is_replicating"] is True
    assert result.metrics["receiver"]["status"] == "streaming"


@pytest.mark.asyncio
async def test_a_replica_without_a_wal_receiver_is_not_streaming():
    result = await check(FakeDriver(replica=True, receiver=None))

    assert not result.healthy
    assert "no WAL receiver" in result.summary
    assert result.metrics["is_replicating"] is False


@pytest.mark.asyncio
async def test_a_receiver_that_is_not_streaming_says_what_it_is_doing():
    result = await check(FakeDriver(replica=True, receiver={"status": "waiting", "sender_host": None, "latest_end_time": None}))

    assert not result.healthy
    assert "its WAL receiver is waiting" in result.summary


@pytest.mark.asyncio
async def test_a_receiver_hidden_from_the_role_is_said_to_be_unreadable_not_stopped():
    """Without pg_read_all_stats every column but the pid reads NULL."""
    result = await check(FakeDriver(replica=True, receiver={"status": None, "sender_host": None, "latest_end_time": None}))

    assert result.healthy
    assert "could not be read" in result.summary and "pg_read_all_stats" in result.summary
    assert result.metrics["is_replicating"] is None


@pytest.mark.asyncio
async def test_a_lag_that_cannot_be_read_is_said_with_the_reason():
    result = await check(FakeDriver(replica=True, receiver=STREAMING, lag_error="function pg_last_wal_receive_lsn is not allowed"))

    assert "Replication lag could not be read: function pg_last_wal_receive_lsn is not allowed" in result.summary
    assert result.metrics["lag_error"] == "function pg_last_wal_receive_lsn is not allowed"
    assert result.metrics["replication_lag_seconds"] is None


@pytest.mark.asyncio
async def test_neons_walproposer_is_not_a_replica():
    """The compute's link to its safekeepers sits in pg_stat_replication too."""
    result = await check(FakeDriver(replica=False, standbys=("walproposer",)))

    assert "No active replicas connected." in result.summary
    assert result.metrics["is_replicating"] is False


@pytest.mark.asyncio
async def test_a_primary_with_a_standby_has_replicas_connected():
    result = await check(FakeDriver(replica=False, standbys=("walproposer", "standby1")))

    assert "Has active replicas connected." in result.summary
    assert "receiver" not in result.metrics
