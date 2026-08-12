import asyncio
import threading
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug import AccessMode
from pg_debug import SyncPgDebug
from pg_debug.errors import ExtensionRequiredError
from pg_debug.model import DEFAULT_MIN_ROWS
from pg_debug.model import RuleId
from pg_debug.sql import DbConnPool


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
