"""The orchestrator: which rules run, in what order the findings come back."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SqlDriver
from pg_debug.errors import InvalidArgumentError
from pg_debug.model import Confidence
from pg_debug.model import ModelFinding
from pg_debug.model import ModelReviewer
from pg_debug.model import RuleId
from pg_debug.model import Severity
from pg_debug.model.reviewer import RULES


def finding(rule: RuleId, **overrides) -> ModelFinding:
    defaults = dict(
        rule=rule,
        confidence=Confidence.PROVEN,
        severity=Severity.LOW,
        schema="public",
        table="orders",
        columns=[],
        summary=f"{rule.value} summary",
        detail="detail",
    )
    return ModelFinding(**{**defaults, **overrides})


@pytest.fixture
def driver():
    """A driver whose session() hands the same driver back, as the real one does."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])

    @asynccontextmanager
    async def session():
        yield driver

    driver.session = session
    return driver


def test_every_rule_is_registered():
    """A rule the registry does not know about can be asked for on the command
    line and then silently do nothing."""
    assert set(RULES) == {r for r in RuleId if r is not RuleId.ALL}


@pytest.mark.asyncio
async def test_only_the_selected_rules_run(driver, monkeypatch):
    ran = []

    async def record(rule_id):
        async def rule(_driver, _options):
            ran.append(rule_id)
            return []

        return rule

    for rule_id in RULES:
        monkeypatch.setitem(RULES, rule_id, (await record(rule_id)))

    await ModelReviewer(driver).review(rules="missing_primary_key,naive_timestamp")

    assert set(ran) == {RuleId.MISSING_PRIMARY_KEY, RuleId.NAIVE_TIMESTAMP}


@pytest.mark.asyncio
async def test_findings_come_back_most_severe_first(driver, monkeypatch):
    async def two_findings(_driver, _options):
        return [
            finding(RuleId.NAIVE_TIMESTAMP, severity=Severity.LOW, table="low"),
            finding(RuleId.NAIVE_TIMESTAMP, severity=Severity.HIGH, table="high"),
        ]

    monkeypatch.setitem(RULES, RuleId.NAIVE_TIMESTAMP, two_findings)

    findings = await ModelReviewer(driver).review(rules="naive_timestamp")

    assert [f.table for f in findings] == ["high", "low"]


@pytest.mark.asyncio
async def test_proven_only_drops_what_was_inferred_from_a_sample(driver, monkeypatch):
    async def mixed(_driver, _options):
        return [
            finding(RuleId.NAIVE_TIMESTAMP, table="fact"),
            finding(RuleId.NAIVE_TIMESTAMP, table="guess", confidence=Confidence.CANDIDATE),
        ]

    monkeypatch.setitem(RULES, RuleId.NAIVE_TIMESTAMP, mixed)

    findings = await ModelReviewer(driver).review(rules="naive_timestamp", proven_only=True)

    assert [f.table for f in findings] == ["fact"]


@pytest.mark.asyncio
async def test_the_whole_review_runs_in_one_session(monkeypatch):
    """Not for state — no rule sets any — but so every rule reads the same
    connection rather than borrowing a different one each time."""
    entered = 0
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[])

    @asynccontextmanager
    async def session():
        nonlocal entered
        entered += 1
        yield driver

    driver.session = session

    await ModelReviewer(driver).review()

    assert entered == 1


@pytest.mark.asyncio
async def test_an_unknown_rule_is_rejected_before_anything_runs(driver):
    with pytest.raises(InvalidArgumentError):
        await ModelReviewer(driver).review(rules="no_such_rule")

    driver.execute_query.assert_not_awaited()
