"""The encoding pg-debug asks the server to send text in."""

from typing import Any
from unittest.mock import patch

import pytest
from psycopg.conninfo import conninfo_to_dict

from pg_debug.db import DbConnPool


async def opened_with(url: str) -> dict[str, Any]:
    """The connection parameters the pool's connections are opened with."""
    with patch("pg_debug.db.driver.AsyncConnectionPool") as built:
        built.return_value.open.side_effect = RuntimeError("stop here")
        with pytest.raises(Exception, match="stop here"):
            await DbConnPool(url).pool_connect()
    return conninfo_to_dict(built.call_args.kwargs["conninfo"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:secret@localhost:5432/db?sslmode=prefer",
        # SQL_ASCII declares that the bytes have no known encoding.
        "postgresql://localhost/db?client_encoding=SQL_ASCII",
        "host=localhost dbname=db client_encoding=sql-ascii",
    ],
)
async def test_text_is_asked_for_in_utf8(url, monkeypatch):
    """Left to libpq, a SQL_ASCII database sends text undecoded: psycopg returned
    bytes, and the server version check failed on them."""
    monkeypatch.delenv("PGCLIENTENCODING", raising=False)

    assert (await opened_with(url)).get("client_encoding") == "UTF8"


@pytest.mark.asyncio
async def test_the_rest_of_the_url_is_kept(monkeypatch):
    monkeypatch.delenv("PGCLIENTENCODING", raising=False)

    assert await opened_with("postgresql://user:secret@db.example.com:6432/app?sslmode=require&application_name=x") == {
        "user": "user",
        "password": "secret",
        "host": "db.example.com",
        "port": "6432",
        "dbname": "app",
        "sslmode": "require",
        "application_name": "x",
        "client_encoding": "UTF8",
    }


@pytest.mark.asyncio
async def test_an_encoding_the_user_named_is_the_one_used(monkeypatch):
    """Data a SQL_ASCII database holds in LATIN1 is read as LATIN1 only if the user says so."""
    monkeypatch.delenv("PGCLIENTENCODING", raising=False)
    assert (await opened_with("postgresql://localhost/db?client_encoding=LATIN1")).get("client_encoding") == "LATIN1"
    assert (await opened_with("postgresql://localhost/db")).get("client_encoding") == "UTF8"

    # libpq reads the variable itself when the URL names no encoding.
    monkeypatch.setenv("PGCLIENTENCODING", "LATIN1")
    assert "client_encoding" not in await opened_with("postgresql://localhost/db")
    monkeypatch.setenv("PGCLIENTENCODING", "SQL_ASCII")
    assert (await opened_with("postgresql://localhost/db")).get("client_encoding") == "UTF8"
