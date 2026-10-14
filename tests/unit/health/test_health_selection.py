import asyncio
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SqlDriver
from pg_debug.errors import ConnectionFailedError
from pg_debug.errors import InvalidArgumentError
from pg_debug.errors import QueryError
from pg_debug.health import DatabaseHealthTool
from pg_debug.health import HealthCheckResult
from pg_debug.health import HealthType


def test_parse_single_type():
    assert HealthType.parse("buffer") == {HealthType.BUFFER}


def test_parse_comma_separated_types():
    assert HealthType.parse("index, vacuum") == {HealthType.INDEX, HealthType.VACUUM}


def test_parse_iterable_of_types():
    assert HealthType.parse([HealthType.INDEX, "vacuum"]) == {HealthType.INDEX, HealthType.VACUUM}


def test_parse_all_expands_to_every_check():
    parsed = HealthType.parse("all")
    assert HealthType.ALL not in parsed
    assert parsed == {t for t in HealthType if t is not HealthType.ALL}


def test_parse_all_absorbs_other_types():
    assert HealthType.parse("index,all") == HealthType.parse("all")


def test_parse_rejects_unknown_type():
    with pytest.raises(InvalidArgumentError, match="Invalid health types"):
        HealthType.parse("index,disk")


def test_parse_rejects_empty_selection():
    with pytest.raises(InvalidArgumentError, match="No health checks selected"):
        HealthType.parse("")


@pytest.mark.asyncio
async def test_run_checks_only_runs_requested_checks():
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with (
        patch("pg_debug.health.tool.BufferHealthCalc") as buffer_cls,
        patch("pg_debug.health.tool.IndexHealthCalc") as index_cls,
    ):
        buffer_cls.return_value.index_hit_rate = AsyncMock(
            return_value=HealthCheckResult(name="Buffer health for indexes", healthy=True, summary="99.0%")
        )
        buffer_cls.return_value.table_hit_rate = AsyncMock(
            return_value=HealthCheckResult(name="Buffer health for tables", healthy=True, summary="98.0%")
        )

        result = await tool.run_checks("buffer")

    assert list(result) == ["Buffer health for indexes", "Buffer health for tables"]
    assert result["Buffer health for indexes"].summary == "99.0%"
    index_cls.assert_not_called()


@pytest.mark.asyncio
async def test_health_renders_checks_as_text():
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))
    results = {"Vacuum health": HealthCheckResult(name="Vacuum health", healthy=True, summary="ok")}

    with patch.object(DatabaseHealthTool, "run_checks", AsyncMock(return_value=results)):
        assert await tool.health("vacuum") == "Vacuum health: ok\n"


@pytest.mark.asyncio
async def test_health_reports_when_nothing_ran():
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with patch.object(DatabaseHealthTool, "run_checks", AsyncMock(return_value={})):
        assert await tool.health("vacuum") == "No health checks were performed."


def buffer_checks(buffer_cls, index_hit_rate, table_hit_rate):
    buffer_cls.return_value.index_hit_rate = AsyncMock(**index_hit_rate)
    buffer_cls.return_value.table_hit_rate = AsyncMock(**table_hit_rate)


TABLES_HEALTHY = {"return_value": HealthCheckResult(name="Buffer health for tables", healthy=True, summary="98.0%")}


@pytest.mark.asyncio
async def test_a_check_that_raises_is_reported_and_the_others_still_run():
    """A lock on one table timed out one check; the rest of the report is
    still worth having, and the failed check says why it has no verdict."""
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with patch("pg_debug.health.tool.BufferHealthCalc") as buffer_cls:
        buffer_checks(buffer_cls, {"side_effect": QueryError("canceling statement due to statement timeout")}, TABLES_HEALTHY)

        result = await tool.run_checks("buffer")

    assert list(result) == ["Buffer health for indexes", "Buffer health for tables"]
    failed = result["Buffer health for indexes"]
    assert failed.healthy is False
    assert failed.error == "canceling statement due to statement timeout"
    assert failed.summary == "Could not run: canceling statement due to statement timeout"
    assert result["Buffer health for tables"].summary == "98.0%"


@pytest.mark.asyncio
async def test_an_unexpected_error_is_reported_with_its_type():
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with patch("pg_debug.health.tool.BufferHealthCalc") as buffer_cls:
        buffer_checks(buffer_cls, {"side_effect": KeyError("rate")}, TABLES_HEALTHY)

        result = await tool.run_checks("buffer")

    assert result["Buffer health for indexes"].error == "KeyError: 'rate'"
    assert result["Buffer health for tables"].error is None


@pytest.mark.asyncio
async def test_a_password_in_an_error_is_masked():
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with patch("pg_debug.health.tool.BufferHealthCalc") as buffer_cls:
        buffer_checks(buffer_cls, {"side_effect": QueryError("lost postgresql://app:hunter2@db/prod")}, TABLES_HEALTHY)

        result = await tool.run_checks("buffer")

    error = result["Buffer health for indexes"].error
    assert error is not None and "hunter2" not in error


@pytest.mark.asyncio
async def test_a_failed_check_is_reported_but_cancellation_still_stops_the_run():
    """Ctrl-C, or a caller's own timeout around the run, must not be turned
    into one more check that could not run."""
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with patch("pg_debug.health.tool.BufferHealthCalc") as buffer_cls:
        buffer_checks(buffer_cls, {"side_effect": QueryError("permission denied")}, {"side_effect": asyncio.CancelledError()})

        with pytest.raises(asyncio.CancelledError):
            await tool.run_checks("buffer")

    buffer_cls.return_value.table_hit_rate.assert_awaited_once()


@pytest.mark.asyncio
async def test_once_the_database_cannot_be_reached_the_remaining_checks_are_not_attempted():
    """Each would wait out the connection timeout to fail the same way."""
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with patch("pg_debug.health.tool.BufferHealthCalc") as buffer_cls:
        buffer_checks(buffer_cls, {"side_effect": ConnectionFailedError("Could not connect to database: refused")}, TABLES_HEALTHY)

        result = await tool.run_checks("buffer")

    buffer_cls.return_value.table_hit_rate.assert_not_awaited()
    assert result["Buffer health for indexes"].error == "Could not connect to database: refused"
    assert result["Buffer health for tables"].error == (
        "not attempted, since the database could not be reached (Could not connect to database: refused)"
    )
    assert all(not check.healthy for check in result.values())
