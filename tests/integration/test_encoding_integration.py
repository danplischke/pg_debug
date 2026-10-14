"""pg-debug on a SQL_ASCII database, which stores text as bytes in no declared encoding.

Left to libpq, a client's encoding is the database's, and on SQL_ASCII psycopg
returns text as ``bytes``. pg-debug reads such a database in UTF-8, unless the
user named the encoding the data is in: in the URL, the environment, the
options, a service file, or ALTER DATABASE or ALTER ROLE ... SET.
"""

from typing import LiteralString
from urllib.parse import quote

import psycopg
import pytest
from psycopg import sql
from utils import sql_ascii_database

from pg_debug import PgDebug
from pg_debug.db import SqlDriver
from pg_debug.db import get_postgres_version
from pg_debug.errors import QueryError


@pytest.fixture
def database(test_postgres_connection_string, monkeypatch):
    """The URL of a SQL_ASCII database, with no encoding named in the environment."""
    for variable in ("PGCLIENTENCODING", "PGOPTIONS"):
        monkeypatch.delenv(variable, raising=False)
    with sql_ascii_database(test_postgres_connection_string[0]) as url:
        yield url


def name_latin1_for(url: str, target: LiteralString) -> None:
    """Make LATIN1 the client encoding of the database at ``url`` with an ALTER ``target`` ... SET."""
    database = sql.Identifier(url.rsplit("/", 1)[1])
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("ALTER " + target + " SET client_encoding = 'LATIN1'").format(database=database))


class TestSqlAscii:
    """One container per version for all of these."""

    @pytest.mark.asyncio
    async def test_text_is_read_and_sent_as_utf8(self, database):
        """Commands that read the version failed, the others printed names as b'...', and a non-ASCII literal could not be sent."""
        async with PgDebug(database) as pg:
            assert await pg.execute_sql("SELECT name, current_setting('client_encoding') AS encoding FROM places") == [
                {"name": "Zürich", "encoding": "UTF8"}
            ]
            assert await pg.execute_sql("SELECT count(*) AS n FROM places WHERE name = 'Zürich'") == [{"n": 1}]
            assert "public" in [schema["schema_name"] for schema in await pg.list_schemas()]
            await pg.review_configuration()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("named_by", ["database", "role", "options"])
    async def test_an_encoding_named_outside_the_url_is_kept(self, database, named_by):
        """Asked for UTF-8 regardless, the server refused to send LATIN1 data that read as LATIN1 before."""
        url = database
        if named_by == "database":
            name_latin1_for(database, "DATABASE {database}")
        elif named_by == "role":
            name_latin1_for(database, "ROLE CURRENT_USER IN DATABASE {database}")
        else:
            url = f"{database}?options={quote('-c client_encoding=LATIN1')}"

        async with PgDebug(url) as pg:
            assert await pg.execute_sql("SELECT label, current_setting('client_encoding') AS encoding FROM legacy") == [
                {"label": "café", "encoding": "LATIN1"}
            ]

    @pytest.mark.asyncio
    async def test_bytes_that_are_not_utf8_name_the_remedy(self, database):
        """The server names the byte; pg-debug says why it asked for UTF-8, and how to name the encoding the data is in."""
        async with PgDebug(database) as pg:
            with pytest.raises(QueryError, match=r'invalid byte sequence for encoding "UTF8": 0xe9.*SQL_ASCII.*\?client_encoding=LATIN1'):
                await pg.execute_sql("SELECT label FROM legacy")

        async with PgDebug(f"{database}?client_encoding=LATIN1") as pg:
            assert await pg.execute_sql("SELECT label FROM legacy") == [{"label": "café"}]

    @pytest.mark.asyncio
    async def test_a_callers_own_connection_must_name_an_encoding(self, database):
        """psycopg returns its text as bytes, which failed with a TypeError that did not say why."""
        async with await psycopg.AsyncConnection.connect(database, autocommit=True) as conn:
            with pytest.raises(QueryError, match=r'client_encoding is SQL_ASCII.*client_encoding="UTF8"'):
                await get_postgres_version(SqlDriver(conn=conn))

        async with await psycopg.AsyncConnection.connect(database, autocommit=True, client_encoding="UTF8") as conn:
            driver = SqlDriver(conn=conn)
            assert await get_postgres_version(driver) >= 12
            rows = await driver.execute_query("SELECT name FROM places")
            assert rows is not None
            assert [row.cells for row in rows] == [{"name": "Zürich"}]
