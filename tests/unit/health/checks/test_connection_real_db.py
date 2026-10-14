"""Connection health against a real server: who is idle in transaction, and what a role without pg_read_all_stats can say."""

from typing import LiteralString
from typing import cast
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from pg_debug import PgDebug
from pg_debug.baseline import health_keys
from pg_debug.db import SafeSqlDriver
from pg_debug.health.checks.connection import CONNECTIONS_SQL
from pg_debug.health.checks.connection import QUERY_CHARS
from pg_debug.health.checks.connection import ConnectionHealthCalc

PLAIN_ROLE = "pgdebug_connection_plain"
IDLE_APPLICATION = "pgdebug_idle_in_transaction"


def as_user(url: str, user: str) -> str:
    """The same server and database, connecting as another role. The test servers trust local connections."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{user}@{parts.hostname}:{parts.port}"))


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


@pytest.fixture
def plain_url(url):
    """A role with no pg_read_all_stats, to which other roles' backends show no type or state."""
    with psycopg.connect(url, autocommit=True) as conn:
        # Roles belong to the server, so one may be left from an earlier run.
        conn.execute(
            sql.SQL("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {}) THEN CREATE ROLE {} LOGIN; END IF; END $$").format(
                sql.Literal(PLAIN_ROLE), sql.Identifier(PLAIN_ROLE)
            )
        )
    try:
        yield as_user(url, PLAIN_ROLE)
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = {}").format(sql.Literal(PLAIN_ROLE)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(PLAIN_ROLE)))


async def check(url: str):
    """Every session idle in transaction is past a limit of nothing; the share is out of reach."""
    async with PgDebug(url) as pg:
        calc = ConnectionHealthCalc(await pg.sql_driver(), max_in_use_share=10.0, max_idle_in_transaction_seconds=0)
        return await calc.connection_health_check()


class TestConnectionHealth:
    @pytest.mark.asyncio
    async def test_a_session_left_idle_in_transaction_is_named_with_its_evidence(self, url):
        async with await psycopg.AsyncConnection.connect(url, application_name=IDLE_APPLICATION) as idle:
            pid, user, database = idle.info.backend_pid, idle.info.user, idle.info.dbname
            await idle.execute("SELECT count(*) FROM pg_class")
            result = await check(url)
            await idle.rollback()

        mine = [f for f in result.findings if f.get("application_name") == IDLE_APPLICATION]
        assert len(mine) == 1, result.summary
        finding = mine[0]
        assert finding["kind"] == "idle_in_transaction"
        assert finding["pid"] == pid
        assert finding["usename"] == user
        assert finding["datname"] == database
        assert finding["idle_in_transaction_seconds"] >= 0
        assert finding["query"] == "SELECT count(*) FROM pg_class"
        assert f"Backend {pid} (role {user}, database {database}, application {IDLE_APPLICATION})" in result.summary
        # Known by the job, not the pid it happened to have this time.
        key = {
            "check": "Connection health",
            "kind": "idle_in_transaction",
            "usename": user,
            "datname": database,
            "application_name": IDLE_APPLICATION,
        }
        assert key in health_keys({result.name: result})

    @pytest.mark.asyncio
    async def test_a_role_that_cannot_see_another_role_s_idle_session_does_not_claim_there_is_none(self, url, plain_url):
        async with await psycopg.AsyncConnection.connect(url, application_name=IDLE_APPLICATION) as idle:
            await idle.execute("SELECT count(*) FROM pg_class")
            result = await check(plain_url)
            await idle.rollback()

        assert result.healthy, result.summary
        assert result.metrics["hidden_backends"] >= 1
        assert "none of the visible ones idle in transaction" in result.summary
        assert "none idle in transaction" not in result.summary

    def test_hidden_backends_are_only_those_with_a_database(self, url, plain_url):
        """The logical replication launcher and a physical WAL sender have a
        role but no database, and are never client connections: counted as
        possible ones, every role without pg_read_all_stats read a range."""
        query = cast(LiteralString, SafeSqlDriver.param_sql_to_query(CONNECTIONS_SQL, [QUERY_CHARS, 60]))
        # One transaction, so both read the same snapshot of pg_stat_activity.
        with psycopg.connect(plain_url) as conn, conn.cursor(row_factory=dict_row) as cursor:
            hidden = cursor.execute(
                "SELECT count(*) FILTER (WHERE datid IS NOT NULL) AS with_database, count(*) FILTER (WHERE datid IS NULL) AS without "
                "FROM pg_stat_activity WHERE backend_type IS NULL AND usesysid IS NOT NULL"
            ).fetchone()
            row = cursor.execute(query).fetchone()

        assert hidden is not None and row is not None
        assert row["hidden_backends"] == hidden["with_database"]
