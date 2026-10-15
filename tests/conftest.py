from collections.abc import Generator

import pytest
from dotenv import load_dotenv
from utils import create_pooled_postgres_container
from utils import create_postgres_container

from pg_debug.db import reset_postgres_version_cache

load_dotenv()


@pytest.fixture(scope="class", params=["postgres:12", "postgres:15", "postgres:16"])
def test_postgres_connection_string(request) -> Generator[tuple[str, str], None, None]:
    yield from create_postgres_container(request.param)


@pytest.fixture(scope="class", params=["postgres:16"])
def test_pooled_postgres(request) -> Generator[tuple[str, str], None, None]:
    """The connection strings to a server and to PgBouncer in front of it.

    One version: what a transaction pooler does to a client does not depend on
    the server's.
    """
    yield from create_pooled_postgres_container(request.param)


@pytest.fixture(autouse=True)
def reset_pg_version_cache():
    """Reset the PostgreSQL version cache before each test."""
    reset_postgres_version_cache()
    yield
