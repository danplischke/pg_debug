"""The server version is asked once per server, not once per process, and read from any release."""

from dataclasses import dataclass
from typing import Any

import pytest

from pg_debug.configuration import ConfigurationReviewer
from pg_debug.db import DbConnPool
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db import get_postgres_version
from pg_debug.db import reset_postgres_version_cache

#: What servers report as server_version and server_version_num.
RELEASES = {
    "9.6.24": "90624",
    "15.8": "150008",
    "17.2 (Debian 17.2-1.pgdg120+1)": "170002",
    "18beta1": "180000",
    "18rc1 (Debian 18~rc1-1.pgdg120+1)": "180000",
    "19devel": "190000",
}


class Connection:
    """A connection of a driver's own, so that two drivers reach two servers."""


class Server(SqlDriver):
    """A driver whose server reports one release, and that keeps what it was asked."""

    def __init__(self, version: str, conn: Any = None):
        super().__init__(conn=conn or Connection())
        self.version = version
        self.asked: list[str] = []

    async def execute_query(self, query, params=None, force_readonly=False, **kwargs) -> list[SqlDriver.RowResult] | None:
        self.asked.append(query)
        if query.strip() == "SHOW server_version":
            return [SqlDriver.RowResult(cells={"server_version": self.version})]
        if query.strip() == "SHOW server_version_num":
            return [SqlDriver.RowResult(cells={"server_version_num": RELEASES[self.version]})]
        if "pg_settings" in query:
            return []
        return [SqlDriver.RowResult(cells={"blks_read": 0, "blks_hit": 0, "checkpoints_timed": 0, "checkpoints_requested": 0})]

    def versions_asked(self) -> int:
        return sum(query.strip().startswith("SHOW server_version") for query in self.asked)


@pytest.mark.asyncio
async def test_the_major_version_of_any_release():
    """A pre-release's version text is no number before a dot: int('18beta1') failed."""
    majors = {version: await get_postgres_version(Server(version)) for version in RELEASES}

    assert majors == {
        "9.6.24": 9,
        "15.8": 15,
        "17.2 (Debian 17.2-1.pgdg120+1)": 17,
        "18beta1": 18,
        "18rc1 (Debian 18~rc1-1.pgdg120+1)": 18,
        "19devel": 19,
    }


@pytest.mark.asyncio
async def test_each_server_reports_its_own_version():
    """One process holding clients of a 15 and a 17 got 15 from both."""
    fifteen = Server("15.8")
    seventeen = Server("17.2 (Debian 17.2-1.pgdg120+1)")

    assert await get_postgres_version(fifteen) == 15
    assert await get_postgres_version(seventeen) == 17, "the second server is not the first"

    assert await get_postgres_version(fifteen) == 15
    assert (fifteen.versions_asked(), seventeen.versions_asked()) == (1, 1), "each server is asked once"

    reset_postgres_version_cache()
    assert await get_postgres_version(fifteen) == 15
    assert fifteen.versions_asked() == 2, "a reset forgets every server"


@dataclass
class Wrapped:
    """A connection wrapper as a dataclass: compared by value, so not hashable."""


class Slotted:
    """A connection wrapper with slots, and so no weak reference."""

    __slots__ = ()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", [Wrapped(), Slotted()], ids=["unhashable", "no-weakref"])
async def test_a_connection_that_cannot_be_kept_is_asked_each_time(conn):
    """Keeping the version for it raised a TypeError, and every version-gated feature failed with it."""
    server = Server("17.2 (Debian 17.2-1.pgdg120+1)", conn=conn)

    assert await get_postgres_version(server) == 17
    assert await get_postgres_version(server) == 17
    assert server.versions_asked() == 2


@pytest.mark.asyncio
async def test_the_configuration_review_reads_each_servers_own_checkpoint_view():
    """PostgreSQL 17 moved the counters to pg_stat_checkpointer, which a 15 does not have."""
    seventeen = Server("17.2 (Debian 17.2-1.pgdg120+1)")
    fifteen = Server("15.8")

    await ConfigurationReviewer(seventeen).review()
    await ConfigurationReviewer(fifteen).review()

    assert any("pg_stat_checkpointer" in query for query in seventeen.asked)
    assert any("pg_stat_bgwriter" in query for query in fifteen.asked)
    assert not any("pg_stat_checkpointer" in query for query in fifteen.asked)


@pytest.mark.asyncio
async def test_restricted_mode_and_a_query_context_share_their_servers_version():
    """Both reach the server through the driver that holds the connection."""
    seventeen = Server("17.2 (Debian 17.2-1.pgdg120+1)")
    fifteen = Server("15.8")
    await get_postgres_version(seventeen)

    assert await get_postgres_version(SafeSqlDriver(fifteen)) == 15
    assert await get_postgres_version(fifteen.with_context(None)) == 15
    assert await get_postgres_version(SafeSqlDriver(SafeSqlDriver(fifteen))) == 15
    assert fifteen.versions_asked() == 1


class Pool:
    """Stands for the psycopg pool a DbConnPool opens, a new one each time it connects."""


class PooledServer(Server):
    """A driver on a DbConnPool, which opens its pool on the first statement, as the real one does."""

    def __init__(self, version: str):
        self.db_pool = DbConnPool("postgresql://localhost/db")
        super().__init__(version, conn=self.db_pool)

    async def execute_query(self, query, params=None, force_readonly=False, **kwargs) -> list[SqlDriver.RowResult] | None:
        if self.db_pool.pool is None:
            self.db_pool.pool = Pool()  # type: ignore[assignment]
        return await super().execute_query(query, params, force_readonly, **kwargs)

    def reconnect(self, version: str) -> None:
        """What pool_connect does after a failure: close the pool and open another, maybe to another server."""
        self.db_pool.pool = Pool()  # type: ignore[assignment]
        self.version = version


@pytest.mark.asyncio
async def test_a_pool_that_reconnects_asks_its_server_again():
    """Behind one URL a server can be upgraded, or failed over to one of another version."""
    server = PooledServer("15.8")

    assert await get_postgres_version(server) == 15
    assert await get_postgres_version(server) == 15
    assert server.versions_asked() == 1, "kept once the first statement opened the pool"

    server.reconnect("17.2 (Debian 17.2-1.pgdg120+1)")

    assert await get_postgres_version(server) == 17
