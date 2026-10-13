import asyncio
import inspect
import sys
import threading
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import SyncPgDebug
from pg_debug.db import DbConnPool
from pg_debug.errors import ExtensionRequiredError
from pg_debug.model import DEFAULT_MIN_ROWS
from pg_debug.model import RuleId
from pg_debug.partition import DEFAULT_MIN_TABLE_BYTES
from pg_debug.plans import DEFAULT_MIN_RATIO


@pytest.fixture
def pool():
    pool = MagicMock(spec=DbConnPool)
    pool.is_valid = True
    pool.close = AsyncMock()
    return pool


@pytest.fixture
def pg(pool):
    client = SyncPgDebug(pool=pool)
    yield client
    client.close()


def test_configuration_is_passed_through():
    pg = SyncPgDebug("postgresql://localhost/db", access_mode="restricted", timeout=7, search_path="reporting")
    try:
        assert pg.client.access_mode is AccessMode.RESTRICTED
        assert pg.client.effective_timeout == 7
        assert pg.client.search_path == "reporting"
    finally:
        pg.close()


def test_calls_run_off_the_calling_thread(pg):
    captured = {}

    async def record_thread():
        captured["thread"] = threading.current_thread().name
        return []

    with patch.object(pg.client, "list_schemas", record_thread):
        assert pg.list_schemas() == []

    assert captured["thread"] != threading.current_thread().name


def test_results_are_returned_verbatim(pg):
    rows = [{"schema_name": "public"}]

    with patch.object(pg.client, "list_schemas", AsyncMock(return_value=rows)):
        assert pg.list_schemas() == rows


def test_arguments_are_forwarded(pg):
    with patch.object(pg.client, "list_objects", AsyncMock(return_value=[])) as list_objects:
        pg.list_objects("reporting", "view")

    list_objects.assert_awaited_once_with("reporting", "view")


def test_keyword_arguments_are_forwarded(pg):
    with patch.object(pg.client, "explain_query", AsyncMock(return_value="plan")) as explain:
        pg.explain_query("SELECT 1", analyze=True)

    explain.assert_awaited_once_with("SELECT 1", analyze=True, hypothetical_indexes=None)


@pytest.mark.parametrize("tuning_method", ["dta", "llm"])
def test_the_method_keyword_reaches_the_client(pg, tuning_method):
    """`method=` must not collide with the wrapper's own signature."""
    with patch.object(pg.client, "analyze_query_indexes", AsyncMock(return_value={})) as analyze:
        pg.analyze_query_indexes(["SELECT 1"], method=tuning_method)

    analyze.assert_awaited_once_with(["SELECT 1"], max_index_size_mb=10000, method=tuning_method, llm_model=None)


def test_the_method_keyword_reaches_the_client_for_workloads(pg):
    with patch.object(pg.client, "analyze_workload_indexes", AsyncMock(return_value={})) as analyze:
        pg.analyze_workload_indexes(method="llm", max_index_size_mb=50)

    analyze.assert_awaited_once_with(max_index_size_mb=50, method="llm", llm_model=None)


def test_exceptions_propagate_to_the_caller(pg):
    with patch.object(pg.client, "get_top_queries", AsyncMock(side_effect=ExtensionRequiredError("pg_stat_statements"))):
        with pytest.raises(ExtensionRequiredError):
            pg.get_top_queries()


def test_the_pool_survives_across_calls(pg):
    """Each call must reuse one loop, or the pool would be bound to a dead one."""
    loops = []

    async def record_loop():
        loops.append(asyncio.get_running_loop())
        return []

    with patch.object(pg.client, "list_schemas", record_loop):
        pg.list_schemas()
        pg.list_schemas()

    assert loops[0] is loops[1]


def test_windows_gets_a_loop_psycopg_can_connect_on(pool, monkeypatch):
    """psycopg's async connections refuse the proactor loop Windows makes by default."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: pytest.fail("the default loop on Windows is the proactor"))
    pg = SyncPgDebug(pool=pool)
    try:
        assert isinstance(pg._loop._loop, asyncio.SelectorEventLoop)
    finally:
        pg.close()


def test_close_shuts_the_loop_down(pool):
    pg = SyncPgDebug(pool=pool)
    pg.close()

    assert pg._loop._loop.is_closed()
    assert not pg._loop._thread.is_alive()


def test_context_manager_closes_its_own_pool():
    with patch("pg_debug.client.DbConnPool") as pool_cls:
        pool_cls.return_value.close = AsyncMock()
        with SyncPgDebug("postgresql://localhost/db"):
            pass

    pool_cls.return_value.close.assert_awaited_once()


def test_close_is_idempotent(pool):
    pg = SyncPgDebug(pool=pool)
    pg.close()
    pg.close()


def test_connect_returns_the_client(pool):
    pool.pool_connect = AsyncMock()
    pg = SyncPgDebug("postgresql://localhost/db", pool=pool)
    try:
        assert pg.connect() is pg
    finally:
        pg.close()


def test_sync_forwards_the_llm_model(pool):
    pg = SyncPgDebug(pool=pool, llm_model="o3-mini")
    try:
        assert pg.client.llm_model == "o3-mini"
    finally:
        pg.close()


def test_review_data_model_delegates_to_the_async_client(pg):
    with patch.object(type(pg.client), "review_data_model", AsyncMock(return_value=[])) as review:
        assert pg.review_data_model(schema="reporting", proven_only=True) == []
        review.assert_awaited_once_with(rules=RuleId.ALL, schema="reporting", min_rows=DEFAULT_MIN_ROWS, proven_only=True)


def test_diagnose_query_delegates_to_the_async_client(pg):
    with patch.object(type(pg.client), "diagnose_query", AsyncMock(return_value=[])) as diagnose:
        assert pg.diagnose_query("SELECT 1") == []
        diagnose.assert_awaited_once_with("SELECT 1", min_ratio=DEFAULT_MIN_RATIO)


def test_suggest_partitions_delegates_to_the_async_client(pg):
    with patch.object(type(pg.client), "suggest_partitions", AsyncMock(return_value=[])) as suggest:
        assert pg.suggest_partitions(min_pruning_share=0.8) == []
        suggest.assert_awaited_once_with(
            sql=None,
            min_table_bytes=DEFAULT_MIN_TABLE_BYTES,
            min_pruning_share=0.8,
            limit=200,
        )


# -- Parity with PgDebug -------------------------------------------------------
#
# SyncPgDebug is correct only while every method PgDebug gains is mirrored here
# with the same parameters. These check that, instead of relying on whoever
# adds the method to remember.

#: PgDebug's coroutines with no blocking twin, and why.
NOT_MIRRORED = {
    # The driver belongs to the event loop SyncPgDebug runs on its own thread;
    # handed out, it would be used from the caller's thread instead.
    "sql_driver",
}

#: Twins whose return types differ on purpose.
OWN_RETURN_TYPE = {
    # Each returns itself, for chaining.
    "connect",
}


def _public_coroutines() -> list[str]:
    return sorted(name for name, member in inspect.getmembers(PgDebug, inspect.iscoroutinefunction) if not name.startswith("_"))


def _parameters(function) -> list[tuple[str, object, object, object]]:
    parameters = list(inspect.signature(function).parameters.values())[1:]  # without self
    return [(p.name, p.kind, p.default, p.annotation) for p in parameters]


def test_every_coroutine_of_pgdebug_has_a_blocking_twin():
    assert [name for name in _public_coroutines() if name not in NOT_MIRRORED and not hasattr(SyncPgDebug, name)] == []


@pytest.mark.parametrize("name", [name for name in _public_coroutines() if name not in NOT_MIRRORED] + ["__init__"])
def test_a_twin_takes_the_same_arguments(name):
    assert _parameters(getattr(SyncPgDebug, name)) == _parameters(getattr(PgDebug, name))


@pytest.mark.parametrize("name", [name for name in _public_coroutines() if name not in NOT_MIRRORED | OWN_RETURN_TYPE])
def test_a_twin_returns_the_same_type(name):
    assert inspect.signature(getattr(SyncPgDebug, name)).return_annotation == inspect.signature(getattr(PgDebug, name)).return_annotation


def test_the_exceptions_are_still_coroutines_of_pgdebug():
    """An entry here that no longer names a method would excuse nothing."""
    assert NOT_MIRRORED | OWN_RETURN_TYPE <= set(_public_coroutines())
