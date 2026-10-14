"""The timeout and search path: set inside each transaction pg-debug runs, never for the session."""

from unittest.mock import patch

import pytest

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import QueryContext
from pg_debug.db import QUERY_MARKER
from pg_debug.db.driver import DbConnPool
from pg_debug.db.driver import build_transaction_setup


def rendered(statement) -> str | None:
    """The statement as SQL, without the marker every statement carries.

    Tagging is asserted on its own below; these tests are about what the
    settings render to.
    """
    return None if statement is None else statement.as_string(None).replace(f"{QUERY_MARKER} ", "")


def test_nothing_to_set_up_by_default():
    assert build_transaction_setup() is None
    assert build_transaction_setup(search_path="", statement_timeout=None) is None
    assert build_transaction_setup(context=QueryContext()) is None


def test_search_path_is_quoted():
    assert rendered(build_transaction_setup(search_path="reporting, public")) == """SELECT set_config('search_path', '"reporting", "public"', true)"""


def test_search_path_quotes_the_user_placeholder():
    assert rendered(build_transaction_setup(search_path="$user,public")) == """SELECT set_config('search_path', '"$user", "public"', true)"""


def test_search_path_ignores_blank_entries():
    assert rendered(build_transaction_setup(search_path=" , public , ")) == """SELECT set_config('search_path', '"public"', true)"""


def test_search_path_with_a_quote_is_escaped_not_injected():
    assert rendered(build_transaction_setup(search_path="evil\"'; DROP TABLE users; --")) == (
        """SELECT set_config('search_path', '"evil""''; DROP TABLE users; --"', true)"""
    )


def test_statement_timeout_is_set_in_seconds():
    assert rendered(build_transaction_setup(statement_timeout=15)) == "SELECT set_config('statement_timeout', '15s', true)"


def test_zero_statement_timeout_disables_the_timeout():
    assert rendered(build_transaction_setup(statement_timeout=0)) == "SELECT set_config('statement_timeout', '0s', true)"


def test_the_settings_then_the_context_then_the_role():
    context = QueryContext(role="app_user", settings={"work_mem": "64MB"})

    assert rendered(build_transaction_setup(statement_timeout=5, search_path="public", context=context)) == (
        "SELECT set_config('statement_timeout', '5s', true), "
        """set_config('search_path', '"public"', true), """
        "set_config('work_mem', '64MB', true), "
        "set_config('role', 'app_user', true)"
    )


def test_the_setup_is_marked_as_ours():
    """It runs in every transaction; unmarked it would look like workload."""
    statement = build_transaction_setup(search_path="public", statement_timeout=5)

    assert statement is not None
    assert statement.as_string(None).startswith(QUERY_MARKER)


@pytest.mark.asyncio
async def test_the_pool_sets_nothing_on_its_connections_and_prepares_nothing():
    """A session SET, or a statement psycopg prepared, belongs to whichever
    backend a transaction pooler gave the connection, and stays there."""
    pool = DbConnPool("postgresql://localhost/db", search_path="reporting", statement_timeout=5)

    with patch("pg_debug.db.driver.AsyncConnectionPool") as built:
        built.return_value.open.side_effect = RuntimeError("stop here")
        with pytest.raises(Exception, match="stop here"):
            await pool.pool_connect()

    options = built.call_args.kwargs
    assert "configure" not in options
    assert options["kwargs"] == {"prepare_threshold": None}


def test_unrestricted_mode_has_no_timeout_by_default():
    assert PgDebug("postgresql://localhost/db").effective_timeout is None


def test_restricted_mode_defaults_to_thirty_seconds():
    assert PgDebug("postgresql://localhost/db", access_mode=AccessMode.RESTRICTED).effective_timeout == 30


@pytest.mark.parametrize("access_mode", [AccessMode.UNRESTRICTED, AccessMode.RESTRICTED])
def test_explicit_timeout_wins_in_both_modes(access_mode):
    assert PgDebug("postgresql://localhost/db", access_mode=access_mode, timeout=7).effective_timeout == 7


def test_client_passes_the_settings_to_its_pool():
    pg = PgDebug("postgresql://localhost/db", timeout=7, search_path="reporting")

    assert pg.pool.search_path == "reporting"
    assert pg.pool.statement_timeout == 7


def test_client_does_not_reconfigure_a_supplied_pool():
    supplied = DbConnPool("postgresql://localhost/db")

    pg = PgDebug(timeout=7, search_path="reporting", pool=supplied)

    assert pg.pool is supplied
    assert supplied.search_path is None
    assert supplied.statement_timeout is None
