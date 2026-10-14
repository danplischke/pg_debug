import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.privileges import StatisticsAccess
from pg_debug.db.privileges import StatisticsRows
from pg_debug.db.privileges import statistics_access


def answering(cells):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells)])
    return driver


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "privilege"), [(15, "USAGE"), (16, "USAGE"), (17, "USAGE"), (14, "MEMBER"), (12, "MEMBER")])
async def test_the_question_is_the_one_the_server_asks_before_showing_a_row(version, privilege):
    """From PostgreSQL 15 the statistics views ask for the privileges of
    pg_read_all_stats, which a member that does not inherit them lacks;
    before, whether the role is a member at all."""
    driver = answering({"role": "app_user", "reads_all_stats": False})

    with patch("pg_debug.db.privileges.get_postgres_version", AsyncMock(return_value=version)):
        await statistics_access(driver)

    query = driver.execute_query.call_args[0][0]
    assert f"pg_has_role(current_user, 'pg_read_all_stats', '{privilege}')" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("reads_all_stats", [True, False])
async def test_the_answer_names_the_role_it_is_about(reads_all_stats):
    driver = answering({"role": "app_user", "reads_all_stats": reads_all_stats})

    with patch("pg_debug.db.privileges.get_postgres_version", AsyncMock(return_value=16)):
        access = await statistics_access(driver)

    assert access == StatisticsAccess(role="app_user", reads_all_stats=reads_all_stats)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [14, 16])
async def test_the_question_is_allowed_in_restricted_mode(version):
    driver = answering({"role": "app_user", "reads_all_stats": True})

    with patch("pg_debug.db.privileges.get_postgres_version", AsyncMock(return_value=version)):
        await statistics_access(driver)

    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(driver.execute_query.call_args[0][0])


def test_rows_with_a_note_are_still_a_list_of_rows():
    """So code that iterated, indexed, compared or serialized them before works as it did."""
    rows = StatisticsRows([{"pid": 1}], hidden="1 of these backends is shown without state.", hidden_count=1)

    assert isinstance(rows, list)
    assert rows == [{"pid": 1}]
    assert rows[0]["pid"] == 1
    assert json.loads(json.dumps(rows)) == [{"pid": 1}]
    assert (rows.hidden, rows.hidden_count) == ("1 of these backends is shown without state.", 1)


def test_rows_without_a_note_say_nothing_was_withheld():
    rows = StatisticsRows()

    assert rows == []
    assert (rows.hidden, rows.hidden_count) == (None, 0)
