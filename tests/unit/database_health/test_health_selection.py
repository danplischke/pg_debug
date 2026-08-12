from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.database_health import DatabaseHealthTool
from pg_debug.database_health import HealthCheckResult
from pg_debug.database_health import HealthType
from pg_debug.errors import InvalidArgumentError
from pg_debug.sql import SqlDriver


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
        patch("pg_debug.database_health.database_health.BufferHealthCalc") as buffer_cls,
        patch("pg_debug.database_health.database_health.IndexHealthCalc") as index_cls,
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


@pytest.mark.asyncio
async def test_run_checks_propagates_driver_errors():
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))

    with patch("pg_debug.database_health.database_health.BufferHealthCalc") as buffer_cls:
        buffer_cls.return_value.index_hit_rate = AsyncMock(side_effect=RuntimeError("connection lost"))

        with pytest.raises(RuntimeError, match="connection lost"):
            await tool.run_checks("buffer")
