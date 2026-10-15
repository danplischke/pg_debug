"""What sessions wait on, against a real server: a lock queue behind DDL, a sampled wait profile, and a migration's lock_timeout.

The server is shared with whatever else runs on it, so every assertion is
about this test's own backends: its pids, or its database.
"""

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import QueryContext
from pg_debug.severity import Confidence

TABLE = "pgdebug_waits_t"


@pytest.fixture
def url(test_postgres_connection_string) -> Iterator[str]:
    url = test_postgres_connection_string[0]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(f"CREATE TABLE {TABLE} (id int PRIMARY KEY, n int)")
        conn.execute(f"INSERT INTO {TABLE} SELECT g, 0 FROM generate_series(1, 10) g")
    try:
        yield url
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")


def database_of(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def wait_until_waiting(url: str, *pids: int) -> None:
    deadline = time.monotonic() + 10
    with psycopg.connect(url, autocommit=True) as conn:
        while True:
            waiting = {row[0] for row in conn.execute("SELECT pid FROM pg_locks WHERE NOT granted AND pid = ANY(%s)", [list(pids)]).fetchall()}
            if waiting == set(pids):
                return
            assert time.monotonic() < deadline, f"only {waiting} of {pids} ever waited"
            time.sleep(0.05)


@dataclass(frozen=True)
class Queue:
    holder: int
    ddl: int
    queued: int


@contextmanager
def ddl_lock_queue(url: str) -> Iterator[Queue]:
    """Three connections: one holds ACCESS SHARE in an open transaction, one waits for ACCESS EXCLUSIVE, one queues behind it."""
    holder = psycopg.connect(url, autocommit=True, application_name="pgdebug_holder")
    ddl = psycopg.connect(url, autocommit=True, application_name="pgdebug_ddl")
    queued = psycopg.connect(url, autocommit=True, application_name="pgdebug_queued")
    pids = Queue(*(conn.info.backend_pid for conn in (holder, ddl, queued)))
    holder.execute("BEGIN")
    holder.execute(f"SELECT * FROM {TABLE}")
    threads = [threading.Thread(target=lambda: ddl.execute(f"ALTER TABLE {TABLE} ADD COLUMN extra int"))]
    threads[0].start()
    try:
        wait_until_waiting(url, pids.ddl)
        threads.append(threading.Thread(target=lambda: queued.execute(f"SELECT count(*) FROM {TABLE}")))
        threads[1].start()
        wait_until_waiting(url, pids.ddl, pids.queued)
        yield pids
    finally:
        holder.execute("ROLLBACK")
        for thread in threads:
            thread.join()
        for conn in (holder, ddl, queued):
            conn.close()


class TestWaits:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("access_mode", [AccessMode.UNRESTRICTED, AccessMode.RESTRICTED])
    async def test_a_queue_behind_a_waiting_ddl_is_classified_from_pg_locks(self, url, access_mode):
        with ddl_lock_queue(url) as pids:
            async with PgDebug(url, access_mode=access_mode) as pg:
                diagnosis = await pg.classify_waits()

        queues = [c for c in diagnosis.classifications if c.kind == "ddl_lock_queue" and c.evidence["waiter"] == pids.ddl]
        assert len(queues) == 1, diagnosis.to_dict()
        queue = queues[0]
        assert queue.confidence is Confidence.PROVEN
        assert queue.evidence["mode"] == "ACCESS EXCLUSIVE"
        assert queue.evidence["relation"] == f"public.{TABLE}"
        assert [holder["pid"] for holder in queue.evidence["holders"]] == [pids.holder]
        assert queue.evidence["holders"][0]["modes"] == ["ACCESS SHARE"]
        assert queue.evidence["queued"] == [{"pid": pids.queued, "mode": "ACCESS SHARE"}]

        idle = [c for c in diagnosis.classifications if c.kind == "idle_in_transaction_blocker" and c.evidence["pid"] == pids.holder]
        assert len(idle) == 1
        assert idle[0].evidence["blocking"] == [pids.ddl]

    @pytest.mark.asyncio
    async def test_the_sampler_reports_samples_taken_and_the_interval_achieved(self, url):
        sleeper = psycopg.connect(url, autocommit=True, application_name="pgdebug_sleeper")
        sleeping = threading.Thread(target=lambda: sleeper.execute("SELECT pg_sleep(4)"))
        sleeping.start()
        try:
            with ddl_lock_queue(url):
                async with PgDebug(url, access_mode=AccessMode.RESTRICTED) as pg:
                    profile = await pg.sample_waits(1.5, interval=0.25)
        finally:
            sleeping.join()
            sleeper.close()

        timing = profile.timing
        assert timing.samples == 6
        assert timing.requested_interval_seconds == 0.25
        # Measured on the server's clock: at least what was asked, and not a
        # multiple of it.
        assert timing.achieved_interval_seconds is not None
        assert 0.24 <= timing.achieved_interval_seconds < 0.5
        assert timing.span_seconds == pytest.approx(timing.achieved_interval_seconds * 5, abs=0.01)

        ours = [query for query in profile.queries if query["datname"] == database_of(url)]
        waits = {wait["wait"] for query in ours for wait in query["waits"]}
        assert "Timeout:PgSleep" in waits
        assert "Lock:relation" in waits
        # The sleeper, the DDL and the queued SELECT were active in every sample.
        assert sum(query["active_sessions"] for query in ours) == pytest.approx(3.0)
        assert all(query["confidence"] == "candidate" for query in ours)
        # pg-debug's own statements are not in it.
        assert not any("pg_debug" in (query["query"] or "") for query in profile.queries)
        assert profile.to_dict()["exactness"] == "each sample is exact; the profile is a sample of the interval"


@contextmanager
def a_role_with_a_lock_timeout(url: str, timeout: str) -> Iterator[str]:
    role = f"pgdebug_waits_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        conn.execute(sql.SQL("ALTER ROLE {} SET lock_timeout = {}").format(sql.Identifier(role), sql.Literal(timeout)))
        conn.execute(sql.SQL("GRANT ALL ON {} TO {}").format(sql.Identifier(TABLE), sql.Identifier(role)))
    try:
        yield role
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(TABLE), sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


class TestMigrationLockTimeout:
    @pytest.mark.asyncio
    async def test_no_lock_timeout_on_an_existing_table_is_advised_one(self, url):
        async with PgDebug(url) as pg:
            [review] = await pg.check_migration(f"ALTER TABLE {TABLE} ADD COLUMN note text")

        assert review.timeouts["lock_timeout"]["milliseconds"] == 0
        assert review.lock_timeout_advice is not None
        assert "SET lock_timeout" in review.lock_timeout_advice

    @pytest.mark.asyncio
    async def test_the_role_the_migration_runs_as_is_read_for_its_own_lock_timeout(self, url):
        with a_role_with_a_lock_timeout(url, "3s") as role:
            async with PgDebug(url, context=QueryContext(role=role)) as pg:
                [review] = await pg.check_migration(f"ALTER TABLE {TABLE} ADD COLUMN note text")

        assert review.timeouts["lock_timeout"]["milliseconds"] == 3000
        assert f"ALTER ROLE {role} SET lock_timeout" in review.timeouts["lock_timeout"]["source"]
        assert review.lock_timeout_advice is None

    @pytest.mark.asyncio
    async def test_a_lock_timeout_in_the_query_context_is_read(self, url):
        async with PgDebug(url, access_mode=AccessMode.RESTRICTED, context=QueryContext(settings={"lock_timeout": "4s"})) as pg:
            [review] = await pg.check_migration(f"ALTER TABLE {TABLE} ADD COLUMN note text")

        assert review.timeouts["lock_timeout"]["milliseconds"] == 4000
        assert review.timeouts["lock_timeout"]["source"] == "set by the query context"
        assert review.lock_timeout_advice is None
