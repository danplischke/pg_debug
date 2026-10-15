"""--static runs only what reads the catalog alone, and refuses to run nothing."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SqlDriver
from pg_debug.errors import InvalidArgumentError
from pg_debug.health import DatabaseHealthTool
from pg_debug.health import HealthCheckResult
from pg_debug.model import ModelReviewer
from pg_debug.model import reviewer


@pytest.mark.asyncio
async def test_static_health_runs_only_the_catalog_checks():
    tool = DatabaseHealthTool(MagicMock(spec=SqlDriver))
    ran: list[str] = []

    def selected(types):
        def check(name):
            async def run():
                ran.append(name)
                return HealthCheckResult(name=name, healthy=True, summary="ok")

            return run

        names = ["Invalid index check", "Duplicate index check", "Index bloat", "Unused index check", "Constraint health", "Table bloat"]
        return [(name, check(name)) for name in names]

    with patch.object(DatabaseHealthTool, "_selected_checks", side_effect=selected):
        results = await tool.run_checks("all", static=True)

    assert list(results) == ran == ["Invalid index check", "Duplicate index check", "Constraint health"]


@pytest.mark.asyncio
async def test_static_health_with_nothing_static_selected_is_refused():
    with pytest.raises(InvalidArgumentError, match="reads only the catalog"):
        await DatabaseHealthTool(MagicMock(spec=SqlDriver)).run_checks("buffer,scan", static=True)


@pytest.mark.asyncio
async def test_static_model_review_runs_only_the_catalog_rules():
    ran: list[str] = []

    def rule(name):
        async def run(driver, options):
            ran.append(name)
            return []

        return run

    driver = MagicMock(spec=SqlDriver)
    driver.session = MagicMock()
    driver.session.return_value.__aenter__ = AsyncMock(return_value=driver)
    driver.session.return_value.__aexit__ = AsyncMock(return_value=None)
    rules = {rule_id: rule(rule_id.value) for rule_id in reviewer.RULES}

    with patch.dict(reviewer.RULES, rules):
        await ModelReviewer(driver).review(static=True)

    assert sorted(ran) == ["blank_padded_char", "foreign_key_type_mismatch", "json_column", "missing_primary_key", "naive_timestamp", "unindexed_foreign_key"]


@pytest.mark.asyncio
async def test_static_model_review_of_sampled_rules_only_is_refused():
    with pytest.raises(InvalidArgumentError, match="nullable_never_null"):
        await ModelReviewer(MagicMock(spec=SqlDriver)).review(rules="nullable_never_null", static=True)
