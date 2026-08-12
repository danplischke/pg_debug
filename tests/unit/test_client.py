from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug.artifacts import ErrorResult
from pg_debug.artifacts import ExplainPlanArtifact
from pg_debug.artifacts import PlanNode
from pg_debug.client import resolve_database_url
from pg_debug.errors import ConnectionFailedError
from pg_debug.errors import ExplainPlanError
from pg_debug.errors import ExtensionRequiredError
from pg_debug.errors import InvalidArgumentError
from pg_debug.sql import DbConnPool
from pg_debug.sql import SafeSqlDriver
from pg_debug.sql import SqlDriver


@pytest.fixture
def pool():
    """A pool that reports itself as connected, so no I/O is attempted."""
    pool = MagicMock(spec=DbConnPool)
    pool.is_valid = True
    pool.close = AsyncMock()
    return pool


@pytest.fixture
def pg(pool):
    return PgDebug(access_mode=AccessMode.UNRESTRICTED, pool=pool)


def make_plan() -> ExplainPlanArtifact:
    node = PlanNode(node_type="Seq Scan", total_cost=10.0, startup_cost=0.0, plan_rows=1, plan_width=4)
    return ExplainPlanArtifact(value='{"Plan": {}}', plan_tree=node)


# -- Connection handling ----------------------------------------------------


def test_resolve_database_url_prefers_argument(monkeypatch):
    monkeypatch.setenv("DATABASE_URI", "postgresql://env/db")
    assert resolve_database_url("postgresql://arg/db") == "postgresql://arg/db"


def test_resolve_database_url_falls_back_to_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URI", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
    assert resolve_database_url() == "postgresql://env/db"


def test_resolve_database_url_without_any_source(monkeypatch):
    monkeypatch.delenv("DATABASE_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(InvalidArgumentError, match="No database URL provided"):
        resolve_database_url()


@pytest.mark.asyncio
async def test_connect_wraps_failures_and_hides_password():
    pool = MagicMock(spec=DbConnPool)
    pool.is_valid = False
    pool.pool_connect = AsyncMock(side_effect=Exception("could not connect to postgresql://user:hunter2@localhost/db"))

    with pytest.raises(ConnectionFailedError) as excinfo:
        await PgDebug("postgresql://user:hunter2@localhost/db", pool=pool).connect()

    assert "hunter2" not in str(excinfo.value)
    assert "****" in str(excinfo.value)


@pytest.mark.asyncio
async def test_close_leaves_caller_supplied_pool_alone(pool):
    async with PgDebug(pool=pool) as pg:
        assert pg.pool is pool
    pool.close.assert_not_called()


@pytest.mark.asyncio
async def test_close_closes_own_pool():
    with patch("pg_debug.client.DbConnPool") as pool_cls:
        pool_cls.return_value.close = AsyncMock()
        async with PgDebug("postgresql://localhost/db"):
            pass
    pool_cls.return_value.close.assert_awaited_once()


# -- Access mode ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "access_mode,expected_type",
    [(AccessMode.UNRESTRICTED, SqlDriver), (AccessMode.RESTRICTED, SafeSqlDriver)],
)
async def test_sql_driver_matches_access_mode(pool, access_mode, expected_type):
    driver = await PgDebug(access_mode=access_mode, pool=pool).sql_driver()
    assert isinstance(driver, expected_type)


@pytest.mark.asyncio
async def test_restricted_driver_uses_configured_timeout(pool):
    driver = await PgDebug(access_mode="restricted", timeout=5, pool=pool).sql_driver()
    assert isinstance(driver, SafeSqlDriver)
    assert driver.timeout == 5


@pytest.mark.asyncio
async def test_sql_driver_connects_when_pool_is_not_valid():
    pool = MagicMock(spec=DbConnPool)
    pool.is_valid = False
    pool.pool_connect = AsyncMock()

    await PgDebug("postgresql://localhost/db", pool=pool).sql_driver()

    pool.pool_connect.assert_awaited_once_with("postgresql://localhost/db")


# -- Queries ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_sql_returns_row_dicts(pg):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={"count": 3})])

    with patch.object(PgDebug, "sql_driver", AsyncMock(return_value=driver)):
        assert await pg.execute_sql("SELECT count(*) FROM orders") == [{"count": 3}]


@pytest.mark.asyncio
async def test_execute_sql_without_result_set(pg):
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=None)

    with patch.object(PgDebug, "sql_driver", AsyncMock(return_value=driver)):
        assert await pg.execute_sql("CREATE TABLE t (id int)") == []


# -- Explain ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_query_returns_plan(pg):
    plan = make_plan()
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.ExplainPlanTool") as tool_cls,
    ):
        tool_cls.return_value.explain = AsyncMock(return_value=plan)
        assert await pg.explain_query("SELECT 1") is plan
        tool_cls.return_value.explain.assert_awaited_once_with("SELECT 1")


@pytest.mark.asyncio
async def test_explain_query_analyze_uses_explain_analyze(pg):
    plan = make_plan()
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.ExplainPlanTool") as tool_cls,
    ):
        tool_cls.return_value.explain_analyze = AsyncMock(return_value=plan)
        await pg.explain_query("SELECT 1", analyze=True)
        tool_cls.return_value.explain_analyze.assert_awaited_once_with("SELECT 1")


@pytest.mark.asyncio
async def test_explain_query_raises_on_planner_error(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.ExplainPlanTool") as tool_cls,
    ):
        tool_cls.return_value.explain = AsyncMock(return_value=ErrorResult('syntax error at or near "SELCT"'))
        with pytest.raises(ExplainPlanError, match="syntax error"):
            await pg.explain_query("SELCT 1")


@pytest.mark.asyncio
async def test_explain_query_rejects_analyze_with_hypothetical_indexes(pg):
    with patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))):
        with pytest.raises(InvalidArgumentError, match="Cannot use analyze and hypothetical indexes together"):
            await pg.explain_query("SELECT 1", analyze=True, hypothetical_indexes=[{"table": "t", "columns": ["c"]}])


@pytest.mark.asyncio
async def test_explain_query_requires_hypopg(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.check_hypopg_installation_status", AsyncMock(return_value=(False, "install hypopg first"))),
    ):
        with pytest.raises(ExtensionRequiredError) as excinfo:
            await pg.explain_query("SELECT 1", hypothetical_indexes=[{"table": "t", "columns": ["c"]}])

    assert excinfo.value.extension == "hypopg"
    assert "install hypopg first" in str(excinfo.value)


@pytest.mark.asyncio
async def test_explain_query_with_hypothetical_indexes(pg):
    plan = make_plan()
    indexes = [{"table": "orders", "columns": ["user_id"]}]
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.check_hypopg_installation_status", AsyncMock(return_value=(True, ""))),
        patch("pg_debug.client.ExplainPlanTool") as tool_cls,
    ):
        tool_cls.return_value.explain_with_hypothetical_indexes = AsyncMock(return_value=plan)
        assert await pg.explain_query("SELECT 1", hypothetical_indexes=indexes) is plan
        tool_cls.return_value.explain_with_hypothetical_indexes.assert_awaited_once_with("SELECT 1", indexes)


# -- Health and top queries -------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_db_health_returns_one_entry_per_check(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.DatabaseHealthTool") as tool_cls,
    ):
        tool_cls.return_value.run_checks = AsyncMock(return_value={"Connection health": "ok"})
        assert await pg.analyze_db_health("connection") == {"Connection health": "ok"}
        tool_cls.return_value.run_checks.assert_awaited_once_with("connection")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sort_by,expected_method,expected_kwargs",
    [
        ("resources", "get_top_resource_queries", {}),
        ("mean_time", "get_top_queries_by_time", {"limit": 5, "sort_by": "mean"}),
        ("total_time", "get_top_queries_by_time", {"limit": 5, "sort_by": "total"}),
    ],
)
async def test_get_top_queries_dispatch(pg, sort_by, expected_method, expected_kwargs):
    rows = [{"query": "SELECT 1"}]
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.TopQueriesCalc") as tool_cls,
    ):
        setattr(tool_cls.return_value, expected_method, AsyncMock(return_value=rows))
        assert await pg.get_top_queries(sort_by=sort_by, limit=5) == rows
        getattr(tool_cls.return_value, expected_method).assert_awaited_once_with(**expected_kwargs)


@pytest.mark.asyncio
async def test_get_top_queries_rejects_unknown_sort(pg):
    with patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))):
        with pytest.raises(InvalidArgumentError, match="Invalid sort criteria"):
            await pg.get_top_queries(sort_by="magic")  # type: ignore[arg-type]


# -- Index tuning -----------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_query_indexes_requires_queries(pg):
    with pytest.raises(InvalidArgumentError, match="non-empty list"):
        await pg.analyze_query_indexes([])


@pytest.mark.asyncio
async def test_analyze_query_indexes_caps_query_count(pg):
    with pytest.raises(InvalidArgumentError, match="up to 10 queries"):
        await pg.analyze_query_indexes([f"SELECT {i}" for i in range(11)])


@pytest.mark.asyncio
async def test_analyze_query_indexes_uses_dta_by_default(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.DatabaseTuningAdvisor") as dta_cls,
        patch("pg_debug.client.TextPresentation") as presentation_cls,
    ):
        presentation_cls.return_value.analyze_queries = AsyncMock(return_value={"recommendations": []})
        await pg.analyze_query_indexes(["SELECT 1"], max_index_size_mb=100)

        dta_cls.assert_called_once()
        presentation_cls.return_value.analyze_queries.assert_awaited_once_with(queries=["SELECT 1"], max_index_size_mb=100)


@pytest.mark.asyncio
async def test_analyze_workload_indexes_with_llm_method(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.LLMOptimizerTool") as llm_cls,
        patch("pg_debug.client.TextPresentation") as presentation_cls,
    ):
        presentation_cls.return_value.analyze_workload = AsyncMock(return_value={"recommendations": []})
        await pg.analyze_workload_indexes(method="llm")

        llm_cls.assert_called_once()


@pytest.mark.asyncio
async def test_index_tuning_rejects_unknown_method(pg):
    with pytest.raises(InvalidArgumentError, match="Unknown index tuning method"):
        await pg.analyze_workload_indexes(method="guess")  # type: ignore[arg-type]


# -- LLM tuning method ------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_method_rejects_multiple_queries_up_front(pg):
    with pytest.raises(InvalidArgumentError, match="one query at a time"):
        await pg.analyze_query_indexes(["SELECT 1", "SELECT 2"], method="llm")


@pytest.mark.asyncio
async def test_llm_method_accepts_a_single_query(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.LLMOptimizerTool"),
        patch("pg_debug.client.TextPresentation") as presentation_cls,
    ):
        presentation_cls.return_value.analyze_queries = AsyncMock(return_value={"recommendations": []})
        await pg.analyze_query_indexes(["SELECT 1"], method="llm")

    presentation_cls.return_value.analyze_queries.assert_awaited_once()


def test_llm_model_defaults_to_a_capable_model(monkeypatch):
    from pg_debug.index.llm_opt import DEFAULT_LLM_MODEL
    from pg_debug.index.llm_opt import llm_model

    monkeypatch.delenv("PG_DEBUG_LLM_MODEL", raising=False)
    assert llm_model() == DEFAULT_LLM_MODEL


def test_llm_model_is_configurable(monkeypatch):
    from pg_debug.index.llm_opt import llm_model

    monkeypatch.setenv("PG_DEBUG_LLM_MODEL", "gpt-4o-mini")
    assert llm_model() == "gpt-4o-mini"


def test_llm_model_defaults_to_the_environment(monkeypatch, pool):
    monkeypatch.delenv("PG_DEBUG_LLM_MODEL", raising=False)
    assert PgDebug(pool=pool).llm_model is None


@pytest.mark.asyncio
async def test_llm_model_from_the_client_reaches_the_optimizer(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.LLMOptimizerTool") as llm_cls,
        patch("pg_debug.client.TextPresentation") as presentation_cls,
    ):
        presentation_cls.return_value.analyze_queries = AsyncMock(return_value={})
        pg.llm_model = "gpt-4o-mini"
        await pg.analyze_query_indexes(["SELECT 1"], method="llm")

    assert llm_cls.call_args.kwargs["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_a_per_call_llm_model_wins(pg):
    with (
        patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))),
        patch("pg_debug.client.LLMOptimizerTool") as llm_cls,
        patch("pg_debug.client.TextPresentation") as presentation_cls,
    ):
        presentation_cls.return_value.analyze_workload = AsyncMock(return_value={})
        pg.llm_model = "gpt-4o"
        await pg.analyze_workload_indexes(method="llm", llm_model="o3-mini")

    assert llm_cls.call_args.kwargs["model"] == "o3-mini"


def test_optimizer_falls_back_to_the_environment(monkeypatch):
    from pg_debug.index.llm_opt import LLMOptimizerTool

    monkeypatch.setenv("PG_DEBUG_LLM_MODEL", "from-env")
    assert LLMOptimizerTool(MagicMock(spec=SqlDriver)).model == "from-env"
    assert LLMOptimizerTool(MagicMock(spec=SqlDriver), model="explicit").model == "explicit"
