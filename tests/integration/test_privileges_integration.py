"""What PostgreSQL withholds from a role without pg_read_all_stats, and the pool a long-lived client keeps, against a real server.

A role without pg_read_all_stats, which pg_monitor includes, is shown other
roles' backends and the server's own processes without their state, query,
wait or timings, and other roles' statements without their text or queryid.
Whether a backend is in a transaction, and the locks it holds, are not
withheld. From PostgreSQL 15 the server asks for the privileges of
pg_read_all_stats rather than the membership, so a member that does not
inherit them is withheld from as well.

Roles belong to the server, not the database: each test makes its own, under
a name of its own, and drops them after it.
"""

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import psycopg
import pytest
from psycopg import sql

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import cli
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.errors import ConnectionFailedError

TABLE = "privileges_probe"


def as_role(url: str, role: str, **params: str) -> str:
    """The same server and database, connecting as another role. The test servers trust local connections."""
    parts = urlsplit(url)
    query = "&".join(f"{name}={value}" for name, value in params.items())
    return urlunsplit(parts._replace(netloc=f"{role}@{parts.hostname}:{parts.port}", query=query))


@dataclass(frozen=True)
class Server:
    url: str
    version: int
    #: A login role that is neither a superuser nor a member of pg_monitor, as an application's own role usually is.
    plain: str
    #: A member of pg_monitor that does not inherit its privileges.
    noinherit: str

    def as_role(self, role: str, **params: str) -> str:
        return as_role(self.url, role, **params)

    def execute(self, statement: sql.Composable | str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url, autocommit=True) as conn:
            cursor = conn.execute(statement if isinstance(statement, sql.Composable) else sql.SQL(statement), params)  # type: ignore[arg-type]
            return cursor.fetchall() if cursor.description else []


@pytest.fixture
def server(test_postgres_connection_string) -> Iterator[Server]:
    url = test_postgres_connection_string[0]
    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(url, autocommit=True) as conn:
        version = conn.info.server_version // 10000
    found = Server(url=url, version=version, plain=f"pgdebug_plain_{suffix}", noinherit=f"pgdebug_noinherit_{suffix}")
    found.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(found.plain)))
    found.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT").format(sql.Identifier(found.noinherit)))
    found.execute(sql.SQL("GRANT pg_monitor TO {}").format(sql.Identifier(found.noinherit)))
    found.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id int)")
    try:
        yield found
    finally:
        found.execute(f"DROP TABLE IF EXISTS {TABLE}")
        for role in (found.plain, found.noinherit):
            found.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", [role])
            found.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@dataclass(frozen=True)
class Backends:
    holder: int
    waiter: int
    idle: int


@contextmanager
def another_roles_backends(server: Server) -> Iterator[Backends]:
    """A superuser's backends: one in a transaction holding a lock, one waiting on it, and one idle."""
    holder = psycopg.connect(server.url, autocommit=True, application_name="pgdebug_holder")
    waiter = psycopg.connect(server.url, autocommit=True, application_name="pgdebug_waiter")
    idle = psycopg.connect(server.url, autocommit=True, application_name="pgdebug_idle")
    pids = Backends(*(conn.info.backend_pid for conn in (holder, waiter, idle)))
    holder.execute("BEGIN")
    holder.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    waiting = threading.Thread(target=lambda: waiter.execute(f"SELECT * FROM {TABLE}"))
    waiting.start()
    try:
        deadline = time.monotonic() + 10
        while not server.execute("SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted", [pids.waiter]):
            assert time.monotonic() < deadline, "the waiter never waited"
            time.sleep(0.05)
        yield pids
    finally:
        holder.execute("ROLLBACK")
        waiting.join()
        for conn in (holder, waiter, idle):
            conn.close()


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestWhatARoleWithoutPgReadAllStatsIsShown:
    """One container per PostgreSQL version for all of these; each test makes its roles and drops them."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("access_mode", [AccessMode.UNRESTRICTED, AccessMode.RESTRICTED])
    async def test_another_roles_backend_in_a_transaction_is_listed_and_an_idle_one_is_not(self, server, access_mode):
        """The reported failure: "(nothing running)" while a superuser's transaction held a lock."""
        with another_roles_backends(server) as pids:
            async with PgDebug(server.as_role(server.plain), access_mode=access_mode) as plain:
                running = await plain.list_activity()
                access = await plain.statistics_access()
                withheld = {backend["pid"]: backend for backend in await plain.withheld_backends()}

        assert type(running) is list
        listed = {row["pid"]: row for row in running}
        assert pids.idle not in listed
        holder = listed[pids.holder]
        assert (holder["state"], holder["query"], holder["in_transaction"]) == (None, HIDDEN_QUERY_TEXT, True)
        assert listed[pids.waiter]["blocked_by"] == [pids.holder]

        assert withheld[pids.holder]["in_transaction"] is True
        assert f"AccessExclusiveLock on public.{TABLE}" in withheld[pids.holder]["locks"]
        assert withheld[pids.holder]["blocking"] == [pids.waiter]
        assert (withheld[pids.idle]["in_transaction"], withheld[pids.idle]["locks"]) == (False, [])

        assert (access.role, access.reads_all_stats, access.member) == (server.plain, False, False)
        assert access.remedy == f"GRANT pg_monitor TO {server.plain}"

    @pytest.mark.asyncio
    async def test_a_withheld_blocker_stays_at_the_root_of_its_lock_tree(self, server):
        with another_roles_backends(server) as pids:
            async with PgDebug(server.as_role(server.plain)) as plain:
                roots = await plain.lock_tree()

        (root,) = [root for root in roots if root["pid"] == pids.holder]
        assert (root["query"], root["in_transaction"]) == (HIDDEN_QUERY_TEXT, True)
        assert [child["pid"] for child in root["blocking"]] == [pids.waiter]

    def test_the_cli_lists_it_and_says_what_it_left_out_on_standard_error(self, server, capsys, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        with another_roles_backends(server) as pids:
            code, out, err = run(["-d", server.as_role(server.plain), "activity"], capsys)
            json_code, json_out, json_err = run(["--json", "-d", server.as_role(server.plain), "activity"], capsys)

        assert code == json_code == 0
        (row,) = [line for line in out.splitlines() if line.startswith(str(pids.holder))]
        assert "in a transaction" in row
        assert f"holds AccessExclusiveLock on public.{TABLE}; blocking {pids.waiter}" in row
        assert err.startswith("pg-debug: ")
        assert "not listed: they show no sign of a transaction" in err
        assert err.rstrip().endswith(f"GRANT pg_monitor TO {server.plain} grants it.")

        # --json prints the rows the library returns, as before; the note goes beside them.
        rows = json.loads(json_out)
        assert isinstance(rows, list) and pids.holder in [row["pid"] for row in rows]
        assert all("locks" not in row for row in rows)
        assert json_err.startswith("pg-debug: ")

    def test_a_superuser_is_shown_everything_and_told_nothing(self, server, capsys, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        with another_roles_backends(server) as pids:
            code, out, err = run(["-d", server.url, "activity"], capsys)

        assert (code, err) == (0, "")
        (row,) = [line for line in out.splitlines() if line.startswith(str(pids.holder))]
        assert "idle in transaction" in row and "LOCK TABLE" in row

    @pytest.mark.asyncio
    async def test_a_member_that_does_not_inherit_is_told_how_to_inherit(self, server):
        """Before PostgreSQL 15 the membership is enough; from 15 the server
        asks for the privileges, and granting pg_monitor again changes nothing."""
        async with PgDebug(server.as_role(server.noinherit)) as member:
            before = await member.statistics_access()
            remedy = before.remedy
            if remedy is not None:
                server.execute(remedy)
            after = await member.statistics_access()
            withheld = await member.withheld_backends()

        if server.version < 15:
            assert (before.reads_all_stats, before.member, before.remedy) == (True, True, None)
        else:
            assert (before.reads_all_stats, before.member) == (False, True)
            if server.version >= 16:
                # The role's INHERIT attribute is only the default for later grants.
                assert remedy == f"GRANT pg_monitor TO {server.noinherit} WITH INHERIT TRUE"
            else:
                assert remedy == f"ALTER ROLE {server.noinherit} INHERIT"
        assert after.reads_all_stats is True
        assert withheld == []

    @pytest.mark.asyncio
    async def test_a_sample_counts_other_roles_statements_rather_than_merging_them(self, server):
        """pg_stat_statements withholds their queryid; keyed by it, they were one
        statement, reported with one statement's counters or a cumulative total."""
        server.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        workload = ["SELECT 1 FROM pg_class LIMIT 1", "SELECT 1 FROM pg_attribute LIMIT 1", "SELECT 1 FROM pg_type LIMIT 1"]
        stop = threading.Event()

        def run_workload():
            with psycopg.connect(server.url, autocommit=True) as conn:
                while not stop.is_set():
                    for statement in workload:
                        conn.execute(statement)  # type: ignore[arg-type]
                    time.sleep(0.02)

        running = threading.Thread(target=run_workload)
        running.start()
        try:
            await asyncio.sleep(0.5)
            async with PgDebug(server.as_role(server.plain)) as plain:
                sample = await plain.sample_top_queries(1.5, sort_by="total_time", limit=1000)
                cumulative = await plain.get_top_queries(sort_by="total_time", limit=10_000)
        finally:
            stop.set()
            running.join()

        assert sample.withheld >= len(workload)
        assert all(row["queryid"] is not None and row["query"] != HIDDEN_QUERY_TEXT for row in sample.queries)
        # Ranked from the start, they are listed, without their text.
        assert any(row["query"] == HIDDEN_QUERY_TEXT for row in cumulative)


class TestAnIdlePool:
    """A long-lived client holds no connection while idle, and says why when it cannot get one back."""

    @pytest.mark.asyncio
    async def test_an_idle_client_holds_no_connection(self, server, monkeypatch):
        """An open connection keeps a scale-to-zero compute from pausing."""
        monkeypatch.setattr("pg_debug.db.driver.POOL_MAX_IDLE", 0.5)
        application = "pgdebug_idle_probe"

        def connections() -> int:
            return server.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", [application])[0][0]

        async with PgDebug(server.as_role(urlsplit(server.url).username or "postgres", application_name=application)) as pg:
            await pg.list_schemas()
            assert connections() == 1

            deadline = time.monotonic() + 10
            while connections() and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
            assert connections() == 0, "the idle connection was not closed"

            await pg.list_schemas()
            assert connections() == 1, "the next call connects again"

    @pytest.mark.asyncio
    async def test_a_refused_reconnect_fails_at_once_with_the_reason(self, server, monkeypatch):
        """It waited out the pool's 30 seconds, and said only that no connection came, as a QueryError."""
        monkeypatch.setattr("pg_debug.db.driver.POOL_MAX_IDLE", 0.5)
        application = "pgdebug_refused_probe"

        def connections() -> int:
            return server.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", [application])[0][0]

        async with PgDebug(server.as_role(server.plain, application_name=application)) as pg:
            await pg.list_schemas()
            deadline = time.monotonic() + 10
            while connections() and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
            assert connections() == 0

            server.execute(sql.SQL("ALTER ROLE {} CONNECTION LIMIT 0").format(sql.Identifier(server.plain)))
            try:
                started = time.monotonic()
                with pytest.raises(ConnectionFailedError, match="too many connections for role"):
                    await pg.list_schemas()
                assert time.monotonic() - started < 10
            finally:
                server.execute(sql.SQL("ALTER ROLE {} CONNECTION LIMIT -1").format(sql.Identifier(server.plain)))

            assert await pg.list_schemas(), "the next call starts a new pool"
