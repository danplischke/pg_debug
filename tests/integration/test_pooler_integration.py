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

import contextlib
import io
from typing import Any
from typing import LiteralString
from typing import NamedTuple

import psycopg
import pytest
from utils import PGBOUNCER_POOL_SIZE

from pg_debug import IndexUseReason
from pg_debug import PgDebug
from pg_debug import cli

#: Matches nearly every row, so the planner reads the table rather than the
#: index on amount, and uses the index only with sequential scans disabled.
MOST_ROWS = "SELECT * FROM pooled_orders WHERE amount > 100"
ONE_CUSTOMER = "SELECT * FROM pooled_orders WHERE customer_id = 7"


class Server(NamedTuple):
    direct: str
    pooled: str


def run(url: str, *args: str) -> tuple[int, str, str]:
    """Run the command line against a database, returning the exit code, the output and the errors."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", "-d", url, *args])
    return code, out.getvalue(), err.getvalue()


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
        conn.execute("DROP TABLE IF EXISTS pooled_orders")
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
            conn.execute("DROP TABLE IF EXISTS pooled_orders")


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
        ],
        ids=["health", "restricted-health", "model", "tune-queries"],
    )
    def test_a_command_that_runs_many_statements_works_twice_in_a_row(self, server, args):
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
    def test_settings_end_with_the_transaction_they_were_made_for(self, server, args):
        """A session SET stays with the backend, for the next client of the same database and role."""
        defaults = on_every_backend(server.pooled, "SELECT current_setting('statement_timeout') || ' ' || current_setting('search_path')")

        code, _, err = run(server.pooled, *args)

        assert code == 0, err
        assert on_every_backend(server.pooled, "SELECT current_setting('statement_timeout') || ' ' || current_setting('search_path')") == defaults
        assert on_every_backend(server.pooled, "SELECT current_setting('work_mem')") == ["4MB"] * PGBOUNCER_POOL_SIZE
        assert on_every_backend(server.pooled, "SELECT current_user") == ["postgres"] * PGBOUNCER_POOL_SIZE

    def test_the_timeout_holds_whichever_backend_the_statement_reaches(self, server):
        for attempt in range(1, PGBOUNCER_POOL_SIZE + 1):
            code, _, err = run(server.pooled, "--timeout", "1", "execute", "SELECT pg_sleep(3)")
            assert code == 1 and "canceling statement due to statement timeout" in err, f"attempt {attempt}: {err or 'the query ran to the end'}"

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
    def test_hypothetical_indexes_do_not_outlive_the_command(self, server, args):
        """HypoPG keeps them in the backend's memory, where another client's EXPLAIN would use them."""
        code, out, err = run(server.pooled, *args)

        assert code == 0, err
        assert "pooled_orders" in out
        assert on_every_backend(server.pooled, "SELECT count(*) FROM hypopg()") == [0] * PGBOUNCER_POOL_SIZE

    def test_a_hypothetical_index_does_not_outlive_a_query_that_failed(self, server):
        code, _, err = run(
            server.pooled, "explain", "--hypothetical-index", "pooled_orders(customer_id)", "SELECT * FROM pooled_orders WHERE no_such_column = 1"
        )

        assert code == 1 and "no_such_column" in err
        assert on_every_backend(server.pooled, "SELECT count(*) FROM hypopg()") == [0] * PGBOUNCER_POOL_SIZE
