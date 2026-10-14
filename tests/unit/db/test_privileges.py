from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db.privileges import HIDDEN_QUERY_TEXT
from pg_debug.db.privileges import WITHHELD_BACKENDS_SQL
from pg_debug.db.privileges import StatisticsAccess
from pg_debug.db.privileges import is_withheld
from pg_debug.db.privileges import remedy_for
from pg_debug.db.privileges import statistics_access
from pg_debug.db.privileges import withheld_backends


def answering(cells):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells=cells)])
    return driver


async def ask(version, cells):
    driver = answering(cells)
    with patch("pg_debug.db.privileges.get_postgres_version", AsyncMock(return_value=version)):
        access = await statistics_access(driver)
    return access, driver.execute_query.call_args[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "privilege"), [(15, "USAGE"), (16, "USAGE"), (17, "USAGE"), (14, "MEMBER"), (12, "MEMBER")])
async def test_the_question_is_the_one_the_server_asks_before_showing_a_row(version, privilege):
    """From PostgreSQL 15 the statistics views ask for the privileges of
    pg_read_all_stats, which a member that does not inherit them lacks;
    before, whether the role is a member at all."""
    _, query = await ask(version, {"role": "app_user", "reads_all_stats": False})

    assert f"pg_has_role(current_user, 'pg_read_all_stats', '{privilege}') AS reads_all_stats" in query
    assert "pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER') AS member" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [12, 14, 15, 16, 17])
async def test_the_questions_are_allowed_in_restricted_mode(version):
    _, query = await ask(version, {"role": "app_user", "reads_all_stats": True})

    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    safe._validate(query)
    safe._validate(WITHHELD_BACKENDS_SQL)


@pytest.mark.asyncio
async def test_grants_that_do_not_pass_the_privileges_on_are_read_from_16():
    """From PostgreSQL 16 whether a member inherits is an option of each grant."""
    _, on_16 = await ask(16, {"role": "app_user", "reads_all_stats": False})
    _, on_15 = await ask(15, {"role": "app_user", "reads_all_stats": False})

    assert "NOT m.inherit_option" in on_16
    assert "inherit_option" not in on_15


@pytest.mark.asyncio
async def test_a_role_shown_everything_needs_no_remedy():
    access, _ = await ask(16, {"role": "monitor", "role_identifier": "monitor", "reads_all_stats": True, "member": True})

    assert access == StatisticsAccess(role="monitor", reads_all_stats=True, member=True, remedy=None)


@pytest.mark.asyncio
async def test_a_role_that_is_no_member_is_told_to_be_granted_pg_monitor():
    access, _ = await ask(16, {"role": "App User", "role_identifier": '"App User"', "reads_all_stats": False, "member": False})

    assert access == StatisticsAccess(role="App User", reads_all_stats=False, member=False, remedy='GRANT pg_monitor TO "App User"')


@pytest.mark.parametrize(
    ("version", "cells", "remedy"),
    [
        # 16 and later: the INHERIT attribute is only the default for later
        # grants, so the grant itself has to pass the privileges on.
        (16, {"inherits": False, "grants_without_inherit": ["pg_monitor"]}, "GRANT pg_monitor TO app WITH INHERIT TRUE"),
        (17, {"inherits": True, "grants_without_inherit": ["monitoring"]}, "GRANT monitoring TO app WITH INHERIT TRUE"),
        # The grant that stops it is further up the chain, out of the role's own grants.
        (16, {"inherits": True, "grants_without_inherit": []}, "GRANT pg_monitor TO app WITH INHERIT TRUE"),
        # 15: whether a member inherits is the member role's attribute.
        (15, {"inherits": False, "grants_without_inherit": []}, "ALTER ROLE app INHERIT"),
        # It inherits, so a role it is a member through does not: a grant
        # made to it directly passes the privileges on.
        (15, {"inherits": True, "grants_without_inherit": []}, "GRANT pg_monitor TO app"),
    ],
)
def test_a_member_that_does_not_inherit_is_told_how_to_inherit_rather_than_to_be_granted_it_again(version, cells, remedy):
    """Granting pg_monitor to a NOINHERIT member changes nothing."""
    row = {"role": "app", "role_identifier": "app", "reads_all_stats": False, "member": True, **cells}

    assert remedy_for(version, row) == remedy


@pytest.mark.asyncio
async def test_the_withheld_backends_are_read_as_the_server_shows_them():
    row = {"pid": 41, "usename": "postgres", "in_transaction": True, "locks": ["AccessExclusiveLock on public.t"], "blocking": [42]}
    driver = answering(row)

    assert await withheld_backends(driver) == [row]
    assert driver.execute_query.call_args[0][0] == WITHHELD_BACKENDS_SQL


def test_whether_a_backend_is_in_a_transaction_is_read_from_what_every_role_is_shown():
    """Its state is withheld; its transaction ID, its snapshot and the lock it
    holds on its own virtual transaction are not."""
    for evidence in ("a.backend_xid IS NOT NULL", "a.backend_xmin IS NOT NULL", "l.locktype = 'virtualxid'"):
        assert evidence in WITHHELD_BACKENDS_SQL


@pytest.mark.parametrize(("query", "withheld"), [(HIDDEN_QUERY_TEXT, True), ("SELECT 1", False), (None, False)])
def test_a_row_is_withheld_when_its_query_says_so(query, withheld):
    assert is_withheld({"query": query}) is withheld
