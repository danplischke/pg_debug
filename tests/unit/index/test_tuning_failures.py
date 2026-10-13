"""What stops an index tuning run, and how it says so.

Tuning used to report failure as an ``error`` key in its result, which
``except PgDebugError`` never sees and a caller who only reads
``recommendations`` takes for "nothing to recommend". It raises instead.
"""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.errors import ExtensionRequiredError
from pg_debug.errors import IndexTuningError
from pg_debug.errors import InvalidArgumentError
from pg_debug.errors import QueryError
from pg_debug.index.base import HIDDEN_QUERY_TEXT
from pg_debug.index.dta import DatabaseTuningAdvisor
from pg_debug.index.presentation import TextPresentation


class Row:
    def __init__(self, cells: dict[str, Any]):
        self.cells = cells


def answering(answers: dict[str, Any]) -> MagicMock:
    """A driver that answers each statement by the first key found in its text."""

    async def execute_query(query, *args, **kwargs):
        for key, answer in answers.items():
            if key in query:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return []

    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=execute_query)
    return driver


ANALYZED = {"pg_stat_user_tables": [Row({"last_analyze": "2024-01-01"})]}


def hypopg(installed: bool):
    return patch(
        "pg_debug.index.base.check_hypopg_installation_status",
        AsyncMock(return_value=(installed, "" if installed else "The hypopg extension is not installed.")),
    )


@pytest.mark.asyncio
async def test_without_hypopg_tuning_is_an_extension_error():
    advisor = DatabaseTuningAdvisor(answering(ANALYZED))

    with hypopg(installed=False), pytest.raises(ExtensionRequiredError) as excinfo:
        await advisor.analyze_workload(query_list=["SELECT * FROM orders WHERE id = 1"])

    assert excinfo.value.extension == "hypopg"
    assert "not installed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_database_never_analyzed_is_refused():
    advisor = DatabaseTuningAdvisor(answering({"pg_stat_user_tables": [Row({"last_analyze": None})]}))

    with hypopg(installed=True), pytest.raises(IndexTuningError, match="Statistics are not up-to-date"):
        await advisor.analyze_workload(query_list=["SELECT * FROM orders WHERE id = 1"])


@pytest.mark.asyncio
async def test_a_workload_with_nothing_usable_says_why():
    """No recommendations would read as "your indexes are fine"."""
    advisor = DatabaseTuningAdvisor(answering(ANALYZED))
    workload = [{"query": HIDDEN_QUERY_TEXT, "queryid": 1, "calls": 100, "avg_exec_time": 10.0}]

    with hypopg(installed=True), pytest.raises(IndexTuningError, match="pg_read_all_stats"):
        await advisor.analyze_workload(workload=workload)


@pytest.mark.asyncio
async def test_an_empty_workload_is_not_a_failure():
    """Nothing in pg_stat_statements over the thresholds is an answer, not an error."""
    advisor = DatabaseTuningAdvisor(answering({**ANALYZED, "pg_stat_statements": []}))

    with hypopg(installed=True):
        session = await advisor.analyze_workload()

    assert session.recommendations == []


@pytest.mark.asyncio
async def test_a_library_error_mid_analysis_comes_through_unchanged():
    error = QueryError("permission denied for table pg_stat_user_tables")
    advisor = DatabaseTuningAdvisor(answering({"pg_stat_user_tables": error}))

    with hypopg(installed=True), pytest.raises(QueryError) as excinfo:
        await advisor.analyze_workload(query_list=["SELECT * FROM orders WHERE id = 1"])

    assert excinfo.value is error


@pytest.mark.asyncio
async def test_anything_else_that_stops_the_analysis_is_an_index_tuning_error():
    advisor = DatabaseTuningAdvisor(answering({"pg_stat_user_tables": RuntimeError("something broke")}))

    with hypopg(installed=True), pytest.raises(IndexTuningError, match=r"^Index tuning failed: something broke$") as excinfo:
        await advisor.analyze_workload(query_list=["SELECT * FROM orders WHERE id = 1"])

    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_presenting_no_queries_is_an_argument_error():
    presentation = TextPresentation(MagicMock(), DatabaseTuningAdvisor(answering(ANALYZED)))

    with pytest.raises(InvalidArgumentError, match="No queries provided"):
        await presentation.analyze_queries([])


@pytest.mark.asyncio
async def test_presentation_lets_a_tuning_failure_through():
    advisor = DatabaseTuningAdvisor(answering({"pg_stat_user_tables": [Row({"last_analyze": None})]}))
    presentation = TextPresentation(MagicMock(), advisor)

    with hypopg(installed=True), pytest.raises(IndexTuningError, match="Statistics are not up-to-date"):
        await presentation.analyze_queries(["SELECT * FROM orders WHERE id = 1"])
