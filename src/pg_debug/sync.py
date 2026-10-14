"""A blocking wrapper around :class:`~pg_debug.client.PgDebug`.

The library is async underneath. This module is for callers that are not —
scripts, notebooks, test suites and synchronous web apps — and do not want to
manage an event loop themselves.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from concurrent.futures import Future
from typing import Any
from typing import TypeVar

from .client import AccessMode
from .client import PgDebug
from .client import SortBy
from .client import TuningMethod
from .configuration import ConfigFinding
from .context import QueryContext
from .db import DbConnPool
from .health import HealthCheckResult
from .health import HealthType
from .index_use import IndexUse
from .migration import StatementReview
from .model import DEFAULT_MIN_ROWS
from .model import ModelFinding
from .model import RuleId
from .partition import DEFAULT_MIN_PRUNING_SHARE
from .partition import DEFAULT_MIN_TABLE_BYTES
from .partition import PartitionCandidate
from .plans import DEFAULT_MIN_RATIO
from .plans import PlanFinding
from .plans import PlanInput
from .plans.tree import ExplainPlanArtifact
from .schema import ObjectType

T = TypeVar("T")


class _LoopThread:
    """A dedicated event loop running on its own thread.

    The connection pool binds to the loop that created it, so every call has to
    run on the same one; a fresh ``asyncio.run()`` per call would strand the
    pool on a closed loop.
    """

    def __init__(self) -> None:
        # psycopg's async connections need a selector loop. Windows' default is
        # the proactor loop, which psycopg refuses to connect on.
        self._loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="pg-debug-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Awaitable[T]) -> T:
        """Run a coroutine on the loop and block until it finishes."""
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return future.result()

    def close(self) -> None:
        """Stop the loop and join its thread."""
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


class SyncPgDebug:
    """The :class:`~pg_debug.client.PgDebug` API, without the ``await``.

    Every method mirrors its async counterpart's arguments, return value and
    exceptions. Close it when you are done — or use it as a context manager —
    so the connection pool and its event loop are released.

    Example:
        ```python
        from pg_debug import SyncPgDebug

        with SyncPgDebug("postgresql://localhost/mydb") as pg:
            for schema in pg.list_schemas():
                print(schema["schema_name"])
        ```
    """

    def __init__(
        self,
        database_url: str | None = None,
        access_mode: AccessMode | str = AccessMode.UNRESTRICTED,
        timeout: int | None = None,
        search_path: str | None = None,
        llm_model: str | None = None,
        pool: DbConnPool | None = None,
        context: QueryContext | None = None,
    ):
        """Takes the same arguments as :class:`~pg_debug.client.PgDebug`."""
        self._closed = False
        self._loop = _LoopThread()
        self._client = PgDebug(
            database_url=database_url,
            access_mode=access_mode,
            timeout=timeout,
            search_path=search_path,
            llm_model=llm_model,
            pool=pool,
            context=context,
        )

    @property
    def client(self) -> PgDebug:
        """The wrapped async client."""
        return self._client

    def _call(self, coroutine_function: Callable[..., Awaitable[T]], /, *args: Any, **kwargs: Any) -> T:
        # Positional-only, so a wrapped method's own keyword arguments (such as
        # `method=`) cannot collide with this signature.
        return self._loop.run(coroutine_function(*args, **kwargs))

    def connect(self) -> SyncPgDebug:
        """Open the connection pool and verify the database is reachable."""
        self._call(self._client.connect)
        return self

    def close(self) -> None:
        """Close the connection pool and stop the background event loop.

        Calling it more than once is a no-op.
        """
        if self._closed:
            return
        try:
            self._call(self._client.close)
        finally:
            self._closed = True
            self._loop.close()

    def __enter__(self) -> SyncPgDebug:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- Schema introspection ------------------------------------------------

    def list_schemas(self) -> list[dict[str, Any]]:
        """List all schemas in the database."""
        return self._call(self._client.list_schemas)

    def list_objects(self, schema_name: str, object_type: str | ObjectType = ObjectType.TABLE) -> list[dict[str, Any]]:
        """List objects of a given type (table, view, sequence, extension)."""
        return self._call(self._client.list_objects, schema_name, object_type)

    def get_object_details(
        self,
        schema_name: str,
        object_name: str,
        object_type: str | ObjectType = ObjectType.TABLE,
    ) -> dict[str, Any]:
        """Describe a single database object, including columns and indexes."""
        return self._call(self._client.get_object_details, schema_name, object_name, object_type)

    # -- Query execution -----------------------------------------------------

    def execute_sql(self, sql: str) -> list[dict[str, Any]]:
        """Run a SQL statement and return its rows."""
        return self._call(self._client.execute_sql, sql)

    def explain_query(
        self,
        sql: str,
        analyze: bool = False,
        hypothetical_indexes: list[dict[str, Any]] | None = None,
    ) -> ExplainPlanArtifact:
        """Return the execution plan for a query."""
        return self._call(self._client.explain_query, sql, analyze=analyze, hypothetical_indexes=hypothetical_indexes)

    # -- Plan diagnosis ------------------------------------------------------

    def diagnose_query(self, sql: str, min_ratio: float = DEFAULT_MIN_RATIO) -> list[PlanFinding]:
        """Run a query and say what is wrong with the plan it produced."""
        return self._call(self._client.diagnose_query, sql, min_ratio=min_ratio)

    def diagnose_plan(self, plan: PlanInput, min_ratio: float = DEFAULT_MIN_RATIO) -> list[PlanFinding]:
        """Diagnose a plan that already exists, checking this database's catalog."""
        return self._call(self._client.diagnose_plan, plan, min_ratio=min_ratio)

    # -- Index use -----------------------------------------------------------

    def explain_index_use(self, sql: str, index: str | None = None) -> list[IndexUse]:
        """Say whether the planner uses each index on the query's tables, and if not, why."""
        return self._call(self._client.explain_index_use, sql, index=index)

    # -- Planner statistics --------------------------------------------------

    def column_stats(self, table: str) -> list[dict[str, Any]]:
        """Report the planner's per-column statistics for a table."""
        return self._call(self._client.column_stats, table)

    def extended_statistics(self, table: str) -> list[dict[str, Any]]:
        """List the extended statistics objects defined on a table."""
        return self._call(self._client.extended_statistics, table)

    # -- Data model ----------------------------------------------------------

    def review_data_model(
        self,
        rules: str | RuleId | Iterable[str | RuleId] = RuleId.ALL,
        schema: str | None = None,
        min_rows: int = DEFAULT_MIN_ROWS,
        proven_only: bool = False,
    ) -> list[ModelFinding]:
        """Review the data model for defects, and say how to fix each one."""
        return self._call(
            self._client.review_data_model,
            rules=rules,
            schema=schema,
            min_rows=min_rows,
            proven_only=proven_only,
        )

    def check_migration(self, sql: str) -> list[StatementReview]:
        """Review a migration before running it."""
        return self._call(self._client.check_migration, sql)

    def review_configuration(self) -> list[ConfigFinding]:
        """Review the server's settings against what it is actually doing."""
        return self._call(self._client.review_configuration)

    # -- Partitioning --------------------------------------------------------

    def suggest_partitions(
        self,
        sql: str | list[str] | None = None,
        min_table_bytes: int = DEFAULT_MIN_TABLE_BYTES,
        min_pruning_share: float = DEFAULT_MIN_PRUNING_SHARE,
        limit: int = 200,
    ) -> list[PartitionCandidate]:
        """Decide whether a table is worth partitioning, from what the workload does."""
        return self._call(
            self._client.suggest_partitions,
            sql=sql,
            min_table_bytes=min_table_bytes,
            min_pruning_share=min_pruning_share,
            limit=limit,
        )

    # -- Live activity -------------------------------------------------------

    def list_activity(
        self,
        include_idle: bool = False,
        min_duration_seconds: float | None = None,
        blocked_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Report what the server is doing right now."""
        return self._call(
            self._client.list_activity,
            include_idle=include_idle,
            min_duration_seconds=min_duration_seconds,
            blocked_only=blocked_only,
        )

    def vacuum_progress(self) -> list[dict[str, Any]]:
        """Report running vacuums and how far along they are."""
        return self._call(self._client.vacuum_progress)

    def lock_tree(self) -> list[dict[str, Any]]:
        """Report who is blocking whom, root blockers first."""
        return self._call(self._client.lock_tree)

    # -- Diagnostics ---------------------------------------------------------

    def analyze_db_health(
        self,
        health_type: str | HealthType | Iterable[str | HealthType] = HealthType.ALL,
    ) -> dict[str, HealthCheckResult]:
        """Run database health checks."""
        return self._call(self._client.analyze_db_health, health_type)

    def get_top_queries(
        self,
        sort_by: SortBy = "resources",
        limit: int = 10,
        interval_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        """Report the most expensive queries recorded by pg_stat_statements."""
        return self._call(self._client.get_top_queries, sort_by=sort_by, limit=limit, interval_seconds=interval_seconds)

    # -- Index tuning --------------------------------------------------------

    def analyze_workload_indexes(
        self,
        max_index_size_mb: int = 10000,
        method: TuningMethod = "dta",
        llm_model: str | None = None,
    ) -> dict[str, Any]:
        """Recommend indexes for the workload recorded by pg_stat_statements."""
        return self._call(
            self._client.analyze_workload_indexes,
            max_index_size_mb=max_index_size_mb,
            method=method,
            llm_model=llm_model,
        )

    def analyze_query_indexes(
        self,
        queries: list[str],
        max_index_size_mb: int = 10000,
        method: TuningMethod = "dta",
        llm_model: str | None = None,
    ) -> dict[str, Any]:
        """Recommend indexes for a specific list of queries."""
        return self._call(
            self._client.analyze_query_indexes,
            queries,
            max_index_size_mb=max_index_size_mb,
            method=method,
            llm_model=llm_model,
        )


__all__ = ["SyncPgDebug"]
