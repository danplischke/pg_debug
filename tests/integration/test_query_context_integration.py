"""The query context against a real server: a role, a row-level security policy and the variable it reads.

The table's owner bypasses row-level security, which is the whole reason a
context is needed: run as the owner, a query sees every tenant's rows and
plans without the policy's predicate, which is not what the application gets.
"""

import contextlib
import io
import time
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import psycopg
import pytest
from psycopg import sql

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import QueryContext
from pg_debug import cli
from pg_debug.errors import ExplainPlanError
from pg_debug.errors import IndexTuningError
from pg_debug.errors import InvalidArgumentError

APP_ROLE = "pgdebug_context_app"
OUTSIDER = "pgdebug_context_outsider"
TENANTS = 20
ROWS = 20_000

COUNT = "SELECT count(*) AS n FROM context_orders"
BY_AMOUNT = "SELECT * FROM context_orders WHERE amount > 49000"


def as_user(url: str, user: str) -> str:
    """The same server and database, connecting as another role. The test servers trust local connections."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{user}@{parts.hostname}:{parts.port}"))


@pytest.fixture
def connection_string(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


@pytest.fixture
def tenants(connection_string):
    """A table only each tenant's own rows of are visible in, to a role that is not its owner."""
    with psycopg.connect(connection_string, autocommit=True) as conn:
        # Roles belong to the server, not the database, so they may be left
        # from an earlier run against the same one.
        for role in (APP_ROLE, OUTSIDER):
            conn.execute(
                sql.SQL("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {}) THEN CREATE ROLE {} LOGIN; END IF; END $$").format(
                    sql.Literal(role), sql.Identifier(role)
                )
            )
        conn.execute("DROP TABLE IF EXISTS context_orders")
        conn.execute("CREATE TABLE context_orders (id bigserial PRIMARY KEY, tenant_id text NOT NULL, amount int NOT NULL)")
        conn.execute(
            sql.SQL("INSERT INTO context_orders (tenant_id, amount) SELECT 't' || (i % {}), (i * 7919) % 50000 FROM generate_series(1, {}) i").format(
                TENANTS, ROWS
            )
        )
        conn.execute("CREATE INDEX context_orders_tenant ON context_orders (tenant_id)")
        conn.execute("ALTER TABLE context_orders ENABLE ROW LEVEL SECURITY")
        conn.execute("CREATE POLICY tenant_isolation ON context_orders USING (tenant_id = current_setting('app.tenant_id'))")
        conn.execute(f"GRANT SELECT ON context_orders TO {APP_ROLE}")
        conn.execute("ANALYZE context_orders")
    try:
        yield connection_string
    finally:
        with psycopg.connect(connection_string, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS context_orders")


def wait_until_analyzed(url: str) -> None:
    """Until pg_stat_user_tables shows the ANALYZE, which the index advisor checks for.

    Before PostgreSQL 15 the statistics collector reports it up to half a
    second late, and the advisor refuses to tune a table it thinks was never
    analyzed.
    """
    deadline = time.monotonic() + 10
    with psycopg.connect(url, autocommit=True) as conn:
        while not conn.execute("SELECT 1 FROM pg_stat_user_tables WHERE relname = 'context_orders' AND last_analyze IS NOT NULL").fetchone():
            assert time.monotonic() < deadline, "the ANALYZE never showed in pg_stat_user_tables"
            time.sleep(0.1)


def tenant(name: str = "t7", **settings: str) -> QueryContext:
    return QueryContext(role=APP_ROLE, settings={"app.tenant_id": name, **settings})


class TestQueryContext:
    """One container per PostgreSQL version for all of these; each test gets the table fresh."""

    @pytest.mark.asyncio
    async def test_the_application_sees_its_tenant_and_the_owner_sees_everything(self, tenants):
        async with PgDebug(tenants) as owner, PgDebug(tenants, context=tenant()) as app:
            assert await owner.execute_sql(COUNT) == [{"n": ROWS}]
            assert await app.execute_sql(f"{COUNT} WHERE current_user = '{APP_ROLE}'") == [{"n": ROWS // TENANTS}]

    @pytest.mark.asyncio
    async def test_the_plan_is_the_one_the_application_gets(self, tenants):
        async with PgDebug(tenants) as owner, PgDebug(tenants, context=tenant()) as app:
            as_owner = await owner.explain_query(BY_AMOUNT)
            as_app = await app.explain_query(BY_AMOUNT)

        assert "current_setting" not in as_owner.value
        assert "current_setting" in as_app.value, "the policy's predicate is missing from the plan"
        assert as_app.plan_tree.plan_rows < as_owner.plan_tree.plan_rows

    @pytest.mark.asyncio
    async def test_diagnose_needs_the_variable_the_policy_reads(self, tenants):
        async with PgDebug(tenants, context=QueryContext(role=APP_ROLE)) as without_variable:
            with pytest.raises(ExplainPlanError, match=r"app\.tenant_id"):
                await without_variable.diagnose_query(COUNT)

        async with PgDebug(tenants, context=tenant()) as app:
            assert isinstance(await app.diagnose_query(COUNT), list)

    @pytest.mark.asyncio
    async def test_nothing_outlives_the_statement_on_its_connection(self, tenants):
        async with PgDebug(tenants, context=tenant(work_mem="7MB")) as app:
            driver = await app.sql_driver()
            async with driver.session():
                inside = await driver.execute_query(
                    "SELECT current_user AS who, current_setting('work_mem') AS work_mem, current_setting('app.tenant_id') AS t", in_context=True
                )
                after = await driver.execute_query(
                    "SELECT current_user AS who, current_setting('work_mem') AS work_mem, current_setting('app.tenant_id', true) AS t"
                )

        assert inside is not None and after is not None
        assert inside[0].cells == {"who": APP_ROLE, "work_mem": "7MB", "t": "t7"}
        assert after[0].cells["who"] != APP_ROLE
        assert after[0].cells["work_mem"] != "7MB"
        assert not after[0].cells["t"]

    @pytest.mark.asyncio
    async def test_restricted_mode_runs_the_query_in_the_context(self, tenants):
        async with PgDebug(tenants, access_mode=AccessMode.RESTRICTED, context=tenant()) as app:
            assert await app.execute_sql(COUNT) == [{"n": ROWS // TENANTS}]

    @pytest.mark.asyncio
    async def test_a_setting_only_the_connecting_user_may_change_is_set_before_the_role_switch(self, tenants):
        """track_io_timing takes a superuser, which the test servers connect as and the application role is not."""
        async with PgDebug(tenants, context=tenant(track_io_timing="on")) as app:
            rows = await app.execute_sql("SELECT current_user AS who, current_setting('track_io_timing') AS timing")

        assert rows == [{"who": APP_ROLE, "timing": "on"}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("context", "reason"),
        [
            (QueryContext(role="pgdebug_no_such_role"), 'role "pgdebug_no_such_role" does not exist'),
            (QueryContext(settings={"wrok_mem": "64MB"}), 'unrecognized configuration parameter "wrok_mem"'),
            (QueryContext(settings={"work_mem": "lots"}), 'invalid value for parameter "work_mem"'),
        ],
    )
    async def test_a_context_the_server_refuses_fails_once_with_the_reason(self, tenants, context, reason):
        async with PgDebug(tenants, context=context) as pg:
            with pytest.raises(InvalidArgumentError, match=f"Could not apply the query context .*{reason}"):
                await pg.execute_sql("SELECT 1")

    @pytest.mark.asyncio
    async def test_a_role_the_connecting_user_is_not_a_member_of_is_refused(self, tenants):
        async with PgDebug(as_user(tenants, OUTSIDER), context=QueryContext(role=APP_ROLE)) as outsider:
            with pytest.raises(InvalidArgumentError, match=f'permission denied to set role "{APP_ROLE}"'):
                await outsider.execute_sql("SELECT 1")

    @pytest.mark.asyncio
    async def test_index_tuning_plans_the_query_as_the_application(self, tenants):
        """A role that may not read the table cannot plan a query on it, so a run
        whose plans ran in that context has nothing it could analyze."""
        with psycopg.connect(tenants, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS hypopg")
        wait_until_analyzed(tenants)

        async with PgDebug(tenants) as owner:
            assert await owner.analyze_query_indexes([BY_AMOUNT])

        with psycopg.connect(tenants, autocommit=True) as conn:
            conn.execute(f"REVOKE SELECT ON context_orders FROM {APP_ROLE}")

        async with PgDebug(tenants, context=tenant()) as app:
            with pytest.raises(IndexTuningError, match=r"could not be planned \(the first: permission denied for table context_orders\)"):
                await app.analyze_query_indexes([BY_AMOUNT])

    def test_the_command_line_runs_the_query_in_the_context_it_announces(self, tenants, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pg-debug.toml").write_text(f'[context]\nrole = "{APP_ROLE}"\n[context.settings]\napp.tenant_id = "t3"\n', encoding="utf-8")

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", "-d", tenants, "execute", COUNT])

        assert code == 0, err.getvalue()
        assert f"query context: role {APP_ROLE}, app.tenant_id=t3" in err.getvalue()
        assert '"n": 1000' in out.getvalue()
