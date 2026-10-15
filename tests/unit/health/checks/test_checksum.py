"""Data checksums: failures counted, and what is said when they cannot be."""

from datetime import UTC
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.health.checks.checksum import FAILURES_SQL
from pg_debug.health.checks.checksum import SETTING_SQL
from pg_debug.health.checks.checksum import ChecksumHealthCalc

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
RESTART = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def row(**overrides: Any) -> dict[str, Any]:
    cells = {
        "database": "app",
        "shared_catalogs": False,
        "checksum_failures": 0,
        "checksum_last_failure": None,
        "database_reset": None,
        "server_reset": RESTART,
        "now": NOW,
    }
    return {**cells, **overrides}


class FakeDriver:
    def __init__(self, rows=(), enabled: bool = True, version: int = 16):
        self.rows = list(rows)
        self.enabled = enabled
        self.version = version
        self.queries: list[str] = []

    async def execute_query(self, query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult]:
        self.queries.append(query)
        if "server_version_num" in query:
            rows = [{"server_version_num": str(self.version * 10000)}]
        elif "data_checksums" in query:
            rows = [{"data_checksums": "on" if self.enabled else "off"}]
        elif "pg_stat_database" in query:
            rows = self.rows
        else:
            raise AssertionError(f"unexpected query: {query}")
        return [SqlDriver.RowResult(cells=cells) for cells in rows]


async def check(**kwargs: Any):
    return await ChecksumHealthCalc(FakeDriver(**kwargs)).checksum_failure_check()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_failures_is_healthy_and_says_over_what():
    result = await check(rows=[row(database=None, shared_catalogs=True), row()])

    assert result.healthy
    assert "no page has failed its checksum in 1 database or the shared catalogs" in result.summary
    assert result.metrics["windows"]["app"]["counting_began_with"] == "restart"


@pytest.mark.asyncio
async def test_a_failure_is_reported_with_when_and_since_when():
    last = datetime(2026, 10, 13, 8, 30, tzinfo=UTC)

    result = await check(rows=[row(checksum_failures=3, checksum_last_failure=last)])

    assert not result.healthy
    finding = result.findings[0]
    assert (finding["kind"], finding["database"], finding["checksum_failures"]) == ("database", "app", 3)
    assert finding["checksum_last_failure"] == last
    assert finding["counting_since"] == RESTART.isoformat()
    assert "Database 'app': 3 failures, the last at 2026-10-13 08:30:00+00:00" in result.summary


@pytest.mark.asyncio
async def test_a_failure_in_the_shared_catalogs_is_named_as_that():
    result = await check(rows=[row(database=None, shared_catalogs=True, checksum_failures=1, checksum_last_failure=NOW)])

    assert result.findings[0]["kind"] == "shared catalogs"
    assert result.findings[0]["database"] is None
    assert "The shared catalogs: 1 failure," in result.summary


@pytest.mark.asyncio
async def test_checksums_off_is_said_not_read_as_clean():
    driver = FakeDriver(enabled=False)

    result = await ChecksumHealthCalc(driver).checksum_failure_check()  # type: ignore[arg-type]

    assert result.healthy
    assert result.metrics == {"data_checksums": False}
    assert "Data checksums are off, so PostgreSQL cannot detect a page corrupted below it" in result.summary
    assert not any("pg_stat_database" in q for q in driver.queries)


@pytest.mark.asyncio
async def test_before_12_the_failures_are_not_counted():
    result = await check(version=11)

    assert "PostgreSQL 11 does not count checksum failures" in result.summary


def test_the_checksum_reads_run_in_restricted_mode():
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    safe._validate(SETTING_SQL)
    safe._validate(FAILURES_SQL)
