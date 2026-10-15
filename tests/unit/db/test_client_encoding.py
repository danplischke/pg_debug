"""The encoding pg-debug asks the server to send text in, and what it says when that fails."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from pg_debug.db import DbConnPool
from pg_debug.db import SqlDriver
from pg_debug.errors import QueryError


class Connection:
    """A pooled connection, reporting the client encoding the server settled on."""

    def __init__(self, client_encoding: str):
        self.info = MagicMock()
        self.info.parameter_status.side_effect = lambda name: client_encoding if name == "client_encoding" else None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[Any]:
        cursor = MagicMock()

        async def execute(query: str) -> None:
            pass

        cursor.execute = execute
        yield cursor


def server_settling_on(default: str) -> tuple[type, list[Any]]:
    """A pool class for a server that reports ``default`` unless the client asks for an encoding at startup.

    ``default`` is what the database's encoding, ALTER DATABASE or ALTER ROLE
    ... SET client_encoding, PGOPTIONS, a service file or
    ``client_encoding=auto`` made of it: pg-debug sees only the outcome.
    """
    opened: list[Any] = []

    class Pool:
        def __init__(self, conninfo: str, **kwargs: Any):
            self.conninfo = conninfo
            self.closed = False
            opened.append(self)

        async def open(self) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

        @asynccontextmanager
        async def connection(self) -> AsyncIterator[Connection]:
            yield Connection(str(conninfo_to_dict(self.conninfo).get("client_encoding") or default))

    return Pool, opened


async def connect(url: str, default: str) -> tuple[DbConnPool, list[Any]]:
    pool_class, opened = server_settling_on(default)
    db_pool = DbConnPool(url)
    with patch("pg_debug.db.driver.AsyncConnectionPool", pool_class):
        assert await db_pool.pool_connect() is opened[-1]
    return db_pool, opened


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["SQL_ASCII", "sql_ascii"])
async def test_a_database_that_sends_sql_ascii_is_asked_for_utf8(reported):
    """Left to libpq, a SQL_ASCII database sends text undecoded: psycopg returned
    bytes, and the server version check failed on them."""
    url = "postgresql://user:secret@db.example.com:6432/app?sslmode=require&application_name=x"

    db_pool, opened = await connect(url, reported)

    assert [pool.conninfo for pool in opened] == [url, opened[1].conninfo]
    assert opened[0].closed, "the connection that read SQL_ASCII is not kept"
    assert conninfo_to_dict(opened[1].conninfo) == {
        "user": "user",
        "password": "secret",
        "host": "db.example.com",
        "port": "6432",
        "dbname": "app",
        "sslmode": "require",
        "application_name": "x",
        "client_encoding": "UTF8",
    }
    assert not opened[1].closed
    assert db_pool.reads_sql_ascii_as_utf8


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["LATIN1", "UTF8", "WIN1252"])
async def test_an_encoding_the_server_settled_on_is_kept(reported):
    """An encoding named by ALTER DATABASE or ALTER ROLE ... SET, PGOPTIONS or a
    service file reaches the server without passing through the URL; asked for
    UTF-8 regardless, a SQL_ASCII database with LATIN1 data that read as
    LATIN1 refused to send it."""
    url = "postgresql://localhost/app"

    db_pool, opened = await connect(url, reported)

    assert [pool.conninfo for pool in opened] == [url], "connected once, as the user asked"
    assert not db_pool.reads_sql_ascii_as_utf8


@pytest.mark.asyncio
async def test_a_reconnect_decides_again():
    """The database may have been given an encoding since."""
    pool_class, opened = server_settling_on("SQL_ASCII")
    db_pool = DbConnPool("postgresql://localhost/app")
    with patch("pg_debug.db.driver.AsyncConnectionPool", pool_class):
        await db_pool.pool_connect()
        db_pool._is_valid = False
        await db_pool.pool_connect()
    assert db_pool.reads_sql_ascii_as_utf8

    pool_class, opened = server_settling_on("LATIN1")
    db_pool._is_valid = False
    with patch("pg_debug.db.driver.AsyncConnectionPool", pool_class):
        await db_pool.pool_connect()
    assert [pool.conninfo for pool in opened] == ["postgresql://localhost/app"]
    assert not db_pool.reads_sql_ascii_as_utf8


def refusal(sqlstate: str, message: str) -> psycopg.Error:
    error = psycopg.errors.lookup(sqlstate)(message)
    assert error.sqlstate == sqlstate
    return error


class Refusing(SqlDriver):
    """A driver on a pool whose every statement the server refuses."""

    def __init__(self, error: psycopg.Error, utf8_over_sql_ascii: bool):
        db_pool = DbConnPool("postgresql://localhost/app")
        db_pool.reads_sql_ascii_as_utf8 = utf8_over_sql_ascii
        db_pool._is_valid = True
        db_pool.pool = MagicMock()
        super().__init__(conn=db_pool)
        self.error = error

    async def _execute_with_connection(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


async def refused(error: psycopg.Error, utf8_over_sql_ascii: bool) -> QueryError:
    with pytest.raises(QueryError) as raised:
        await Refusing(error, utf8_over_sql_ascii).execute_query("SELECT label FROM legacy")
    assert raised.value.__cause__ is error
    return raised.value


@pytest.mark.asyncio
async def test_a_byte_that_is_not_utf8_names_the_remedy_when_pg_debug_chose_utf8():
    """The server's refusal names the byte; only pg-debug knows it chose UTF-8,
    and that the user can name the encoding the data is in."""
    not_utf8 = 'invalid byte sequence for encoding "UTF8": 0xe9'

    message = str(await refused(refusal("22021", not_utf8), utf8_over_sql_ascii=True))
    assert message.startswith(f"{not_utf8}. The database is SQL_ASCII")
    assert "?client_encoding=LATIN1" in message
    assert "PGCLIENTENCODING" in message

    # An encoding the user chose, or any other refusal: the server's word is the whole story.
    assert str(await refused(refusal("22021", not_utf8), utf8_over_sql_ascii=False)) == not_utf8
    assert str(await refused(refusal("42P01", 'relation "legacy" does not exist'), utf8_over_sql_ascii=True)) == 'relation "legacy" does not exist'


class SqlAsciiConnection(psycopg.AsyncConnection):
    """A caller's own connection whose client encoding is SQL_ASCII, never connected."""

    def __init__(self, client_encoding: str):
        # Nothing of psycopg's is set up: no statement may reach it.
        self._reported = client_encoding

    @property
    def info(self) -> Any:  # type: ignore[override]
        info = MagicMock()
        info.parameter_status.side_effect = lambda name: self._reported if name == "client_encoding" else None
        return info

    def cursor(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise AssertionError("nothing may run on it")


@pytest.mark.asyncio
@pytest.mark.parametrize("run", ["execute_query", "execute_query_and_rollback"])
async def test_a_callers_own_sql_ascii_connection_is_refused_with_the_cause(run):
    """psycopg returns its text as bytes: names printed as b'public', and a
    version check or a join of names failed with a bare TypeError."""
    driver = SqlDriver(conn=SqlAsciiConnection("SQL_ASCII"))

    with pytest.raises(QueryError, match=r'client_encoding is SQL_ASCII.*client_encoding="UTF8"'):
        await getattr(driver, run)("SELECT nspname FROM pg_namespace")
