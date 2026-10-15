"""pg-debug behind a transaction pooler.

PgBouncer in transaction mode, without prepared statements, is what Neon,
Supabase and the poolers Cloud SQL, AlloyDB and Azure offer put in front of
PostgreSQL. Each transaction a client runs may reach a different backend, and
whatever a transaction leaves in its backend's session (a SET, a prepared
statement, a hypothetical index) stays there for the next client of the same
database and role. ``tests/utils.py`` configures the pooler with round robin,
which makes every transaction move to the next backend instead of only the
ones that meet concurrent load.

A test checks what pg-debug left behind by holding a transaction open on every
one of the pooler's backends at once, which is the only way to be sure of
reaching each of them.
"""

from typing import Any
from typing import LiteralString
from typing import NamedTuple

import psycopg
import pytest
from psycopg import sql
from utils import PGBOUNCER_POOL_SIZE
from utils import sql_ascii_database

from pg_debug import AccessMode
from pg_debug import IndexUseReason
from pg_debug import PgDebug
from pg_debug import cli
from pg_debug.errors import ExplainPlanError

#: Matches nearly every row, so the planner reads the table rather than the
#: index on amount, and uses the index only with sequential scans disabled.
MOST_ROWS = "SELECT * FROM pooled_orders WHERE amount > 100"
ONE_CUSTOMER = "SELECT * FROM pooled_orders WHERE customer_id = 7"
#: Planning it waits for a lock on pooled_customers, after the hypothetical
#: index on pooled_orders has been created.
WITH_CUSTOMER = "SELECT * FROM pooled_orders o JOIN pooled_customers c ON c.id = o.customer_id WHERE o.customer_id = 7"


class Server(NamedTuple):
    direct: str
    pooled: str


@pytest.fixture
def run(capsys):
    """Run the command line against a database, returning the exit code, the output and the errors.

    Read through capsys rather than a redirect: with pytest's live logging on,
    as CI runs it, each log record puts pytest's own stream back on
    sys.stderr, and a redirect loses whatever is written after it.
    """

    def run(url: str, *args: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = cli.main(["--json", "-d", url, *args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def on_every_backend(pooled: str, query: LiteralString) -> list[Any]:
    """The first column of a query's first row, from each of the pooler's backends.

    Every connection holds its backend in an open transaction until all have
    run the query, so the pooler cannot hand two of them the same one. The
    transactions are rolled back, not abandoned: a client that leaves in the
    middle of one makes PgBouncer close the backend, and with it whatever a
    later check in the same test would have found there.
    """
    connections = [psycopg.connect(pooled) for _ in range(PGBOUNCER_POOL_SIZE)]
    try:
        values, backends = [], set()
        for conn in connections:
            row = conn.execute(f"SELECT ({query}), pg_backend_pid()").fetchone()
            assert row is not None
            values.append(row[0])
            backends.add(row[1])
        assert len(backends) == PGBOUNCER_POOL_SIZE, "the check did not reach every backend"
        return values
    finally:
        for conn in connections:
            conn.rollback()
            conn.close()


@pytest.fixture
def server(test_pooled_postgres, monkeypatch, tmp_path):
    """A table to work on, behind a pooler with every backend open and new."""
    direct, pooled = test_pooled_postgres
    # Where no pg-debug.toml can change the context the commands run with.
    monkeypatch.chdir(tmp_path)
    with psycopg.connect(direct, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS hypopg")
        conn.execute("DROP TABLE IF EXISTS pooled_orders, pooled_customers")
        conn.execute("CREATE TABLE pooled_customers (id int PRIMARY KEY)")
        conn.execute("CREATE TABLE pooled_orders (id bigserial PRIMARY KEY, customer_id int NOT NULL, amount int NOT NULL)")
        conn.execute("INSERT INTO pooled_orders (customer_id, amount) SELECT i % 1000, (i * 7919) % 50000 FROM generate_series(1, 20000) i")
        conn.execute("CREATE INDEX pooled_orders_amount ON pooled_orders (amount)")
        conn.execute("ANALYZE pooled_orders")
    # So that what one test leaves on a backend cannot fail the next.
    with psycopg.connect(pooled.rsplit("/", 1)[0] + "/pgbouncer", autocommit=True) as admin:
        admin.execute("RECONNECT")
    # The pooler opens a backend only when every open one is busy, and one
    # client running one statement at a time never makes it open a second.
    # Under load it has them all, and round robin has something to rotate.
    on_every_backend(pooled, "SELECT 1")
    try:
        yield Server(direct, pooled)
    finally:
        with psycopg.connect(direct, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS pooled_orders, pooled_customers")


def reconnect(pooled: str) -> None:
    """Have the pooler replace its backends, which keep the client encoding the database gave them when they opened."""
    with psycopg.connect(pooled.rsplit("/", 1)[0] + "/pgbouncer", autocommit=True) as admin:
        admin.execute("RECONNECT")


@pytest.fixture
def sql_ascii(test_pooled_postgres, monkeypatch):
    """A SQL_ASCII database, directly and behind the pooler, with no encoding named in the environment."""
    for variable in ("PGCLIENTENCODING", "PGOPTIONS"):
        monkeypatch.delenv(variable, raising=False)
    direct, pooled = test_pooled_postgres
    with sql_ascii_database(direct) as database:
        name = database.rsplit("/", 1)[1]
        reconnect(pooled)
        yield Server(database, f"{pooled.rsplit('/', 1)[0]}/{name}")


def verdicts(uses) -> list[tuple[str, bool, list[str]]]:
    """Each index's verdict, without the one on fresh statistics: the counters
    it reads arrive in their own time, and the forced plan is what is tested."""
    return [(use.index, use.used, [f.reason.value for f in use.findings if f.reason is not IndexUseReason.STALE_STATISTICS]) for use in uses]


class TestBehindATransactionPooler:
    """One container for all of these."""

    @pytest.mark.parametrize(
        "args",
        [
            ["health"],
            ["--access-mode", "restricted", "health"],
            ["model"],
            ["tune-queries", ONE_CUSTOMER],
            ["--access-mode", "restricted", "activity", "--sample", "1", "--sample-interval", "0.1"],
        ],
        ids=["health", "restricted-health", "model", "tune-queries", "activity-sample"],
    )
    def test_a_command_that_runs_many_statements_works_twice_in_a_row(self, server, run, args):
        """psycopg prepares a statement it has run five times on one connection,
        and the pooler sends the next execution to a backend that has not got it,
        or the next preparation to one that has."""
        for attempt in (1, 2):
            code, _, err = run(server.pooled, *args)
            assert code == 0, f"attempt {attempt}: {err}"

    @pytest.mark.parametrize(
        "args",
        [
            ["--timeout", "2", "--search-path", "public", "execute", "--set", "work_mem=7MB", "SELECT 1"],
            ["--access-mode", "restricted", "execute", "SELECT 1"],
        ],
        ids=["unrestricted", "restricted"],
    )
    def test_settings_end_with_the_transaction_they_were_made_for(self, server, run, args):
        """A session SET stays with the backend, for the next client of the same database and role."""
        defaults = on_every_backend(server.pooled, "SELECT current_setting('statement_timeout') || ' ' || current_setting('search_path')")

        code, _, err = run(server.pooled, *args)

        assert code == 0, err
        assert on_every_backend(server.pooled, "SELECT current_setting('statement_timeout') || ' ' || current_setting('search_path')") == defaults
        assert on_every_backend(server.pooled, "SELECT current_setting('work_mem')") == ["4MB"] * PGBOUNCER_POOL_SIZE
        assert on_every_backend(server.pooled, "SELECT current_user") == ["postgres"] * PGBOUNCER_POOL_SIZE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("access_mode", [AccessMode.UNRESTRICTED, AccessMode.RESTRICTED])
    async def test_every_statement_runs_with_the_timeout_and_the_search_path(self, server, access_mode):
        """Read back by each statement, over more statements than the pooler has
        backends: a SET reaches only the backend it ran on, and with round robin
        the next statement goes to another."""
        statements = 2 * PGBOUNCER_POOL_SIZE
        async with PgDebug(server.pooled, access_mode=access_mode, timeout=7, search_path="public") as pg:
            seen = [
                await pg.execute_sql("SELECT current_setting('statement_timeout') AS timeout, current_setting('search_path') AS path")
                for _ in range(statements)
            ]

        assert seen == [[{"timeout": "7s", "path": '"public"'}]] * statements

    def test_the_timeout_cancels_a_statement_behind_the_pooler(self, server, run):
        """A smoke test. It passes with session SETs too, which the driver ran
        again whenever a timed-out statement made it rebuild its pool: the
        test above, which reads the setting back, is the one that tells."""
        code, _, err = run(server.pooled, "--timeout", "1", "execute", "SELECT pg_sleep(3)")

        assert code == 1 and "canceling statement due to statement timeout" in err, err or "the query ran to the end"

    @pytest.mark.asyncio
    async def test_index_use_gives_the_direct_verdict_and_leaves_sequential_scans_enabled(self, server):
        async with PgDebug(server.direct) as direct:
            expected = verdicts(await direct.explain_index_use(MOST_ROWS, index="pooled_orders_amount"))
        assert expected == [("pooled_orders_amount", False, ["planner_preferred_other"])], "the query no longer tests the forced plan"

        for attempt in (1, 2, 3):
            async with PgDebug(server.pooled) as pooled:
                assert verdicts(await pooled.explain_index_use(MOST_ROWS, index="pooled_orders_amount")) == expected, f"attempt {attempt}"

        assert on_every_backend(server.pooled, "SELECT current_setting('enable_seqscan')") == ["on"] * PGBOUNCER_POOL_SIZE

    @pytest.mark.parametrize(
        "args",
        [
            ["explain", "--hypothetical-index", "pooled_orders(customer_id)", ONE_CUSTOMER],
            ["tune-queries", ONE_CUSTOMER],
        ],
        ids=["explain", "tune-queries"],
    )
    def test_hypothetical_indexes_do_not_outlive_the_command(self, server, run, args):
        """HypoPG keeps them in the backend's memory, where another client's EXPLAIN would use them."""
        code, out, err = run(server.pooled, *args)

        assert code == 0, err
        assert "pooled_orders" in out
        assert on_every_backend(server.pooled, "SELECT count(*) FROM hypopg()") == [0] * PGBOUNCER_POOL_SIZE

    def test_a_hypothetical_index_does_not_outlive_a_query_that_failed(self, server, run):
        code, _, err = run(
            server.pooled, "explain", "--hypothetical-index", "pooled_orders(customer_id)", "SELECT * FROM pooled_orders WHERE no_such_column = 1"
        )

        assert code == 1 and "no_such_column" in err
        assert on_every_backend(server.pooled, "SELECT count(*) FROM hypopg()") == [0] * PGBOUNCER_POOL_SIZE

    @pytest.mark.asyncio
    async def test_a_hypothetical_index_does_not_outlive_a_cancelled_plan(self, server):
        """Restricted mode's own timeout cancels the statement from this side,
        which reaches the driver as a cancellation, not as an error PostgreSQL
        raised. The plan waits for a lock another session holds, after the
        hypothetical index has been created."""
        with psycopg.connect(server.direct) as locker:
            locker.execute("LOCK TABLE pooled_customers IN ACCESS EXCLUSIVE MODE")
            async with PgDebug(server.pooled, access_mode=AccessMode.RESTRICTED, timeout=1) as pg:
                with pytest.raises(ExplainPlanError, match=r"timed out|statement timeout"):
                    await pg.explain_query(WITH_CUSTOMER, hypothetical_indexes=[{"table": "pooled_orders", "columns": ["customer_id"]}])

        assert on_every_backend(server.pooled, "SELECT count(*) FROM hypopg()") == [0] * PGBOUNCER_POOL_SIZE

    @pytest.mark.asyncio
    async def test_a_sql_ascii_database_is_read_in_utf8_and_left_as_it_was_for_other_clients(self, sql_ascii):
        """pg-debug asks for UTF-8 when it connects, not with a SET: the pooler
        keeps a client's startup parameters for that client, and puts each
        client's own back on whichever backend serves it."""
        encoding = "SELECT current_setting('client_encoding')"
        # Read by a client that named no encoding, so the bytes come back undecoded.
        assert on_every_backend(sql_ascii.pooled, encoding) == [b"SQL_ASCII"] * PGBOUNCER_POOL_SIZE

        async with PgDebug(sql_ascii.pooled) as pg:
            # Round robin takes these to every backend.
            for _ in range(2 * PGBOUNCER_POOL_SIZE):
                assert await pg.execute_sql("SELECT name, current_setting('client_encoding') AS encoding FROM places") == [
                    {"name": "Zürich", "encoding": "UTF8"}
                ]

        assert on_every_backend(sql_ascii.pooled, encoding) == [b"SQL_ASCII"] * PGBOUNCER_POOL_SIZE

    @pytest.mark.asyncio
    async def test_an_encoding_the_database_names_is_kept_behind_the_pooler(self, sql_ascii):
        """The pooler reports what its backends were given, and pg-debug takes it rather than ask for UTF-8 over it."""
        with psycopg.connect(sql_ascii.direct, autocommit=True) as conn:
            conn.execute(sql.SQL("ALTER DATABASE {} SET client_encoding = 'LATIN1'").format(sql.Identifier(sql_ascii.direct.rsplit("/", 1)[1])))
        reconnect(sql_ascii.pooled)

        async with PgDebug(sql_ascii.pooled) as pg:
            assert await pg.execute_sql("SELECT label, current_setting('client_encoding') AS encoding FROM legacy") == [
                {"label": "café", "encoding": "LATIN1"}
            ]
