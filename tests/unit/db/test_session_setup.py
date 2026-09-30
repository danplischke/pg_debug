from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug.db import QUERY_MARKER
from pg_debug.db.driver import DbConnPool
from pg_debug.db.driver import build_session_setup


def rendered(statements):
    """The statements as SQL, without the marker every statement carries.

    Tagging is asserted on its own below; these tests are about what the
    settings render to.
    """
    return [statement.as_string(None).replace(f"{QUERY_MARKER} ", "") for statement in statements or []]


def test_no_session_setup_by_default():
    assert build_session_setup() is None
    assert build_session_setup(search_path="", statement_timeout=None) is None


def test_search_path_is_quoted():
    assert rendered(build_session_setup(search_path="reporting, public")) == ['SET search_path TO "reporting", "public"']


def test_search_path_quotes_the_user_placeholder():
    assert rendered(build_session_setup(search_path="$user,public")) == ['SET search_path TO "$user", "public"']


def test_search_path_ignores_blank_entries():
    assert rendered(build_session_setup(search_path=" , public , ")) == ['SET search_path TO "public"']


def test_search_path_with_a_quote_is_escaped_not_injected():
    statements = rendered(build_session_setup(search_path='evil"; DROP TABLE users; --'))
    assert statements == ['SET search_path TO "evil""; DROP TABLE users; --"']


def test_statement_timeout_is_set_in_seconds():
    assert rendered(build_session_setup(statement_timeout=15)) == ["SET statement_timeout = '15s'"]


def test_zero_statement_timeout_disables_the_timeout():
    assert rendered(build_session_setup(statement_timeout=0)) == ["SET statement_timeout = '0s'"]


def test_both_settings_are_applied():
    assert rendered(build_session_setup(search_path="public", statement_timeout=5)) == [
        'SET search_path TO "public"',
        "SET statement_timeout = '5s'",
    ]


@pytest.mark.asyncio
async def test_pool_configures_each_connection():
    pool = DbConnPool("postgresql://localhost/db", search_path="reporting", statement_timeout=5)
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.commit = AsyncMock()

    await pool._configure_connection(conn)

    assert rendered([call.args[0] for call in conn.execute.call_args_list]) == [
        'SET search_path TO "reporting"',
        "SET statement_timeout = '5s'",
    ]
    conn.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_skips_configuration_when_nothing_is_set():
    pool = DbConnPool("postgresql://localhost/db")
    conn = MagicMock()
    conn.execute = AsyncMock()

    await pool._configure_connection(conn)

    conn.execute.assert_not_called()


def test_unrestricted_mode_has_no_timeout_by_default():
    assert PgDebug("postgresql://localhost/db").effective_timeout is None


def test_restricted_mode_defaults_to_thirty_seconds():
    assert PgDebug("postgresql://localhost/db", access_mode=AccessMode.RESTRICTED).effective_timeout == 30


@pytest.mark.parametrize("access_mode", [AccessMode.UNRESTRICTED, AccessMode.RESTRICTED])
def test_explicit_timeout_wins_in_both_modes(access_mode):
    assert PgDebug("postgresql://localhost/db", access_mode=access_mode, timeout=7).effective_timeout == 7


def test_client_passes_session_settings_to_its_pool():
    pg = PgDebug("postgresql://localhost/db", timeout=7, search_path="reporting")

    assert pg.pool.search_path == "reporting"
    assert pg.pool.statement_timeout == 7


def test_client_does_not_reconfigure_a_supplied_pool():
    supplied = DbConnPool("postgresql://localhost/db")

    pg = PgDebug(timeout=7, search_path="reporting", pool=supplied)

    assert pg.pool is supplied
    assert supplied.search_path is None
    assert supplied.statement_timeout is None


def test_session_setup_is_marked_as_ours():
    """These run on every pooled connection; unmarked they look like workload."""
    statements = build_session_setup(search_path="public", statement_timeout=5)

    assert statements is not None
    assert all(statement.as_string(None).startswith(QUERY_MARKER) for statement in statements)
