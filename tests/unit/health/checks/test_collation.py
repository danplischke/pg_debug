"""Collation versions: a recorded version the library no longer reports, and the indexes on it."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.health.checks.collation import COLLATIONS_SQL
from pg_debug.health.checks.collation import DATABASES_SQL
from pg_debug.health.checks.collation import INDEXES_SQL
from pg_debug.health.checks.collation import CollationHealthCalc


def database(**overrides: Any) -> dict[str, Any]:
    cells = {"database": "app", "identifier": "app", "is_current": True, "recorded_version": "2.31", "actual_version": "2.31"}
    return {**cells, **overrides}


def collation(**overrides: Any) -> dict[str, Any]:
    cells = {
        "schema": "public",
        "collation": "de-x-icu",
        "identifier": 'public."de-x-icu"',
        "provider": "icu",
        "recorded_version": "153.14",
        "actual_version": "153.14",
        "collation_oid": 12345,
    }
    return {**cells, **overrides}


class FakeDriver:
    def __init__(self, databases=(), collations=(), indexes=None, version: int = 16):
        self.databases = list(databases)
        self.collations = list(collations)
        self.indexes = indexes or {}
        self.version = version
        self.queries: list[str] = []

    async def execute_query(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        if "server_version_num" in query:
            rows = [{"server_version_num": str(self.version * 10000)}]
        elif "FROM pg_database" in query:
            rows = self.databases
        elif "FROM pg_collation" in query:
            rows = self.collations
        elif "FROM pg_index" in query:
            oid = int(query.split("WHERE")[1].split("=")[0].strip())
            rows = self.indexes.get(oid, [])
        else:
            raise AssertionError(f"unexpected query: {query}")
        return [SqlDriver.RowResult(cells=cells) for cells in rows]


async def check(**kwargs: Any):
    return await CollationHealthCalc(FakeDriver(**kwargs)).collation_version_check()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_matching_versions_are_healthy_and_say_what_was_compared():
    result = await check(databases=[database()], collations=[collation()])

    assert result.healthy
    assert "compared 1 collation used by this database's indexes and the default collation of 1 database" in result.summary
    assert result.metrics["collations_examined_names"] == ["public.de-x-icu"]


@pytest.mark.asyncio
async def test_a_drifted_collation_names_the_indexes_that_depend_on_it_and_the_fix():
    indexes = {12345: [{"index": "public.users_name_idx", "table": "public.users"}]}

    result = await check(collations=[collation(recorded_version="153.14", actual_version="153.80")], indexes=indexes)

    assert not result.healthy
    finding = result.findings[0]
    assert finding["kind"] == "collation"
    assert finding["collation"] == "de-x-icu"
    assert finding["index_count"] == 1
    assert finding["fix"] == ["REINDEX INDEX CONCURRENTLY public.users_name_idx;", 'ALTER COLLATION public."de-x-icu" REFRESH VERSION;']
    assert "Collation 'public.de-x-icu', icu (recorded 153.14, library now 153.80): 1 index depends on it: public.users_name_idx." in result.summary
    assert "not that an index is corrupt" in result.summary


@pytest.mark.asyncio
async def test_the_connected_databases_default_collation_lists_the_indexes_on_the_default():
    indexes = {100: [{"index": "public.users_email_idx", "table": "public.users"}]}

    result = await check(databases=[database(recorded_version="2.28", actual_version="2.36")], indexes=indexes)

    finding = result.findings[0]
    assert finding["kind"] == "database"
    assert finding["fix"][-1] == "ALTER DATABASE app REFRESH COLLATION VERSION;"
    assert finding["fix"][0] == "REINDEX INDEX CONCURRENTLY public.users_email_idx;"


@pytest.mark.asyncio
async def test_another_databases_drift_is_reported_without_guessing_its_indexes():
    result = await check(databases=[database(database="other", identifier="other", is_current=False, recorded_version="2.28", actual_version="2.36")])

    finding = result.findings[0]
    assert finding["indexes"] is None
    assert "connect to it to list the indexes that use it" in result.summary


@pytest.mark.asyncio
async def test_no_version_from_the_library_proves_nothing():
    result = await check(collations=[collation(actual_version=None)])

    assert result.healthy


@pytest.mark.asyncio
async def test_before_15_the_database_default_is_not_read_and_that_is_said():
    driver = FakeDriver(collations=[collation()], version=14)

    result = await CollationHealthCalc(driver).collation_version_check()  # type: ignore[arg-type]

    assert not any("FROM pg_database" in q for q in driver.queries)
    assert "PostgreSQL 14 does not record the database default collation's version" in result.summary


def test_only_collations_an_index_uses_are_asked_about():
    assert "indcollation" in COLLATIONS_SQL
    assert "c.collversion IS NOT NULL" in COLLATIONS_SQL


def test_the_collation_reads_run_in_restricted_mode():
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    for sql in (DATABASES_SQL, COLLATIONS_SQL, INDEXES_SQL.format(100)):
        safe._validate(sql)
