"""The server version is asked once per server, not once per process."""

from typing import Any

import pytest

from pg_debug.configuration import ConfigurationReviewer
from pg_debug.db import DbConnPool
from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db import get_postgres_version
from pg_debug.db import reset_postgres_version_cache


class Connection:
    """A connection of a driver's own, so that two drivers reach two servers."""


class Server(SqlDriver):
    """A driver whose server reports one version, and that keeps what it was asked."""

    def __init__(self, version: str, conn: Any = None):
        super().__init__(conn=conn or Connection())
        self.version = version
        self.asked: list[str] = []

    async def execute_query(self, query, params=None, force_readonly=False, **kwargs) -> list[SqlDriver.RowResult] | None:
        self.asked.append(query)
        if query.strip() == "SHOW server_version":
            return [SqlDriver.RowResult(cells={"server_version": self.version})]
        if "pg_settings" in query:
            return []
        return [SqlDriver.RowResult(cells={"blks_read": 0, "blks_hit": 0, "checkpoints_timed": 0, "checkpoints_requested": 0})]

    def versions_asked(self) -> int:
        return sum(query.strip() == "SHOW server_version" for query in self.asked)


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


@pytest.mark.asyncio
async def test_the_configuration_review_reads_each_servers_own_checkpoint_view():
    """PostgreSQL 17 moved the counters to pg_stat_checkpointer, which a 15 does not have."""
    seventeen = Server("17.2")
    fifteen = Server("15.8")

    await ConfigurationReviewer(seventeen).review()
    await ConfigurationReviewer(fifteen).review()

    assert any("pg_stat_checkpointer" in query for query in seventeen.asked)
    assert any("pg_stat_bgwriter" in query for query in fifteen.asked)
    assert not any("pg_stat_checkpointer" in query for query in fifteen.asked)


@pytest.mark.asyncio
async def test_restricted_mode_and_a_query_context_share_their_servers_version():
    """Both reach the server through the driver that holds the connection."""
    seventeen = Server("17.2")
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

    server.reconnect("17.2")

    assert await get_postgres_version(server) == 17
