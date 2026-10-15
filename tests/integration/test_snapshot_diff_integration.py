"""Measuring over an interval against a real server: server rates, resets inside the window, culprits first, changed statements."""

import asyncio
import threading
import time
import uuid

import psycopg
import pytest
from psycopg import sql

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug.top_queries import MIN_WINDOW_FACTOR

TABLE = "pgdebug_rates_t"
#: The sampling interval. The patched sleep is asyncio's own, so it acts only
#: on a call for exactly this long: the sampler's, not the pool's.
INTERVAL = 1.0001


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


@pytest.fixture
def table(url):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(f"CREATE TABLE {TABLE} (id int)")
    try:
        yield TABLE
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")


@pytest.fixture
def slot(url):
    """A physical slot that reserves WAL now, dropped afterwards: other tests share the server."""
    name = f"pgdebug_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_create_physical_replication_slot(%s, true)", [name])
    try:
        yield name
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT pg_drop_replication_slot(%s)", [name])


def write_during(url: str, seconds: float, *, reset: bool = False):
    """Return a sleep that commits rows (and resets this database's counters, if asked) while pg-debug waits.

    The writing connection closes before the sleep ends, so its counters are
    reported before the second snapshot.
    """
    real_sleep = asyncio.sleep

    async def sleep(asked: float, *args, **kwargs) -> None:
        if asked != INTERVAL:
            return await real_sleep(asked, *args, **kwargs)

        def work() -> None:
            with psycopg.connect(url, autocommit=True) as conn:
                if reset:
                    conn.execute("SELECT pg_stat_reset()")
                for i in range(50):
                    conn.execute(f"INSERT INTO {TABLE} SELECT generate_series(1, 100) + %s", [i])

        await asyncio.to_thread(work)
        await real_sleep(seconds)

    return sleep


@pytest.mark.asyncio
@pytest.mark.parametrize("access_mode", [AccessMode.UNRESTRICTED, AccessMode.RESTRICTED])
async def test_rates_are_measured_over_the_interval(url, table, slot, access_mode, monkeypatch):
    monkeypatch.setattr("pg_debug.server_rates.asyncio.sleep", write_during(url, 1.5))
    async with PgDebug(url, access_mode=access_mode) as pg:
        rates = await pg.sample_server_rates(INTERVAL)

    database = rates.group("database")
    assert database is not None and database.refused is None
    assert database.interval_seconds is not None and database.interval_seconds >= 1.5
    assert (database.rate("xact_commit") or 0) >= 50 / database.interval_seconds
    assert (database.rate("tup_inserted") or 0) > 0
    position = rates.group("wal_position", "written")
    assert position is not None and (position.rate("bytes") or 0) > 0
    if rates.server_version >= 14:
        wal = rates.group("wal")
        assert wal is not None and (wal.rate("wal_records") or 0) > 0
    checkpoints = rates.group("checkpoints")
    assert checkpoints is not None and checkpoints.refused is None
    kept = rates.group("replication_slot", slot)
    assert kept is not None and kept.refused is None
    (retained,) = kept.counters
    # The slot holds back everything written since it was made.
    assert retained.after is not None and retained.after > 0
    assert (retained.delta or 0) > 0
    assert kept.details["slot_type"] == "physical"


@pytest.mark.asyncio
async def test_a_reset_inside_the_interval_refuses_the_database_counters(url, table, monkeypatch):
    """Without the refusal, xact_commit reads as a negative rate, or as the few commits since the reset."""
    with psycopg.connect(url, autocommit=True) as conn:
        for _ in range(20):
            conn.execute(f"INSERT INTO {TABLE} VALUES (1)")
    time.sleep(1.2)

    monkeypatch.setattr("pg_debug.server_rates.asyncio.sleep", write_during(url, 1.2, reset=True))
    async with PgDebug(url) as pg:
        rates = await pg.sample_server_rates(INTERVAL)

    database = rates.group("database")
    assert database is not None
    assert database.counters == []
    assert database.refused is not None and "reset during the interval" in database.refused
    assert database.window is not None and database.window["counting_began_with"] == "reset"


@pytest.mark.asyncio
async def test_activity_lists_the_root_blocker_first_with_everything_behind_it(url, table):
    root = psycopg.connect(url)
    root.execute(f"LOCK {TABLE} IN ACCESS EXCLUSIVE MODE")
    pids: list[int] = []

    def wait(mode: str) -> None:
        with psycopg.connect(url) as conn:
            row = conn.execute("SELECT pg_backend_pid()").fetchone()
            assert row is not None
            pids.append(row[0])
            conn.execute(sql.SQL("LOCK {} IN {} MODE").format(sql.Identifier(TABLE), sql.SQL(mode)))  # type: ignore[arg-type]
            conn.rollback()

    first = threading.Thread(target=wait, args=("ACCESS EXCLUSIVE",))
    first.start()
    time.sleep(0.5)
    second = threading.Thread(target=wait, args=("ACCESS SHARE",))
    second.start()
    time.sleep(0.5)
    try:
        root_row = root.execute("SELECT pg_backend_pid()").fetchone()
        assert root_row is not None
        async with PgDebug(url, access_mode=AccessMode.RESTRICTED) as pg:
            rows = await pg.list_activity()
    finally:
        root.rollback()
        root.close()
        first.join()
        second.join()

    involved = [row for row in rows if row["pid"] in (root_row[0], *pids)]
    assert [row["pid"] for row in involved] == [root_row[0], *pids]
    assert [row["blocking_total"] for row in involved] == [2, 1, 0]
    assert rows[0]["pid"] == root_row[0]


@pytest.mark.asyncio
async def test_a_statement_first_seen_in_the_sample_is_new_or_the_judgement_is_refused(url, monkeypatch):
    """New only when pg_stat_statements has counted much longer than the sample; otherwise it says why not."""
    marker = f"pgdebug_new_{uuid.uuid4().hex[:8]}"
    real_sleep = asyncio.sleep

    async def sleep(asked: float, *args, **kwargs) -> None:
        if asked != INTERVAL:
            return await real_sleep(asked, *args, **kwargs)

        def work() -> None:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(marker)))

        await asyncio.to_thread(work)
        await real_sleep(asked)

    # A table of its own makes a statement of its own: pg_stat_statements
    # ignores aliases and constants when it tells statements apart.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        conn.execute(sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier(marker)))
    monkeypatch.setattr("pg_debug.top_queries.asyncio.sleep", sleep)
    try:
        async with PgDebug(url) as pg:
            sample = await pg.sample_top_queries(INTERVAL, sort_by="total_time", limit=1000)
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(marker)))

    (row,) = [row for row in sample.queries if marker in (row.get("query") or "")]
    window = sample.statement_window or {}
    seconds = window.get("counting_seconds")
    if seconds is not None and seconds - INTERVAL >= MIN_WINDOW_FACTOR * INTERVAL:
        assert sample.changes_refused is None
        assert row["change"]["kind"] == "new"
        assert row["change"]["confidence"] == "candidate"
    else:
        assert sample.changes_refused is not None
        assert row["change"] is None
