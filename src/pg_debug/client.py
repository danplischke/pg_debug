"""High-level client for inspecting and tuning a PostgreSQL database."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from enum import Enum
from typing import Any
from typing import Literal

from .activity import ActivityInspector
from .audit import AuditReport
from .audit import SecurityAuditor
from .audit import UpgradeChecker
from .configuration import ConfigFinding
from .configuration import ConfigurationReviewer
from .context import QueryContext
from .db import DbConnPool
from .db import SafeSqlDriver
from .db import SqlDriver
from .db import check_hypopg_installation_status
from .db import obfuscate_password
from .db import privileges
from .db.driver import check_statement_timeout
from .db.privileges import StatisticsAccess
from .db.statements import locate_pg_stat_statements
from .db.window import checkpoint_window
from .db.window import statement_window
from .db.window import table_window
from .errors import ConnectionFailedError
from .errors import ExtensionRequiredError
from .errors import InvalidArgumentError
from .errors import PgDebugError
from .health import DatabaseHealthTool
from .health import HealthCheckResult
from .health import HealthType
from .health.checks.index import MIN_WINDOW_DAYS
from .history import HistoryReader
from .history import HistoryReport
from .index.base import MAX_NUM_INDEX_TUNING_QUERIES
from .index.base import IndexTuningBase
from .index.dta import DatabaseTuningAdvisor
from .index.presentation import TextPresentation
from .index_use import IndexUse
from .index_use import IndexUseAnalyzer
from .migration import MigrationLinter
from .migration import StatementReview
from .model import DEFAULT_MIN_ROWS
from .model import ModelFinding
from .model import ModelReviewer
from .model import RuleId
from .partition import DEFAULT_MIN_PRUNING_SHARE
from .partition import DEFAULT_MIN_TABLE_BYTES
from .partition import PartitionAdvisor
from .partition import PartitionCandidate
from .plans import DEFAULT_MIN_RATIO
from .plans import ExplainPlanTool
from .plans import PlanDiagnoser
from .plans import PlanFinding
from .plans import PlanInput
from .plans.tree import ExplainPlanArtifact
from .platform import PlatformDetection
from .platform import detect_from_url
from .platform import detect_platform
from .pooler import PoolerReport
from .pooler import check_admin_url
from .pooler import inspect_pgbouncer
from .pooler import read_server_connections
from .schema import ObjectType
from .schema import SchemaInspector
from .server_rates import DEFAULT_RATES_INTERVAL
from .server_rates import ServerRates
from .server_rates import ServerRatesSampler
from .sharding import DEFAULT_INTERVAL_SECONDS
from .sharding import ShardReadiness
from .sharding import ShardReadinessAssessor
from .statistics import StatisticsInspector
from .top_queries import QuerySample
from .top_queries import TopQueriesCalc
from .waits import DEFAULT_SAMPLE_INTERVAL
from .waits import WaitDiagnosis
from .waits import WaitProfile
from .waits import WaitSampler
from .waits import classify_waits

logger = logging.getLogger(__name__)

#: Environment variables consulted, in order, when no database URL is passed.
DATABASE_URL_ENV_VARS = ("DATABASE_URI", "DATABASE_URL")

#: Default statement timeout, in seconds, applied in restricted mode.
DEFAULT_RESTRICTED_TIMEOUT = 30

TuningMethod = Literal["dta", "llm"]
SortBy = Literal["resources", "mean_time", "total_time"]


def _check_sort_by(sort_by: str) -> None:
    if sort_by not in ("resources", "mean_time", "total_time"):
        raise InvalidArgumentError("Invalid sort criteria. Please use 'resources', 'mean_time' or 'total_time'.")


class AccessMode(str, Enum):
    """How much the client is allowed to do to the database.

    Attributes:
        UNRESTRICTED: Any statement runs as-is, including writes and DDL.
        RESTRICTED: Statements are parsed and only read-only ones are allowed,
            run inside a read-only transaction with a statement timeout.
    """

    UNRESTRICTED = "unrestricted"
    RESTRICTED = "restricted"


def resolve_database_url(database_url: str | None = None) -> str:
    """Return the database URL to connect to.

    Falls back to the ``DATABASE_URI`` then ``DATABASE_URL`` environment
    variables.

    Raises:
        InvalidArgumentError: If no URL was given and neither variable is set.
    """
    if database_url:
        return database_url

    for var in DATABASE_URL_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value

    raise InvalidArgumentError(
        "No database URL provided. Pass one explicitly or set the " + " or ".join(DATABASE_URL_ENV_VARS) + " environment variable."
    )


class PgDebug:
    """Inspect, diagnose and tune a PostgreSQL database.

    The client owns a connection pool and exposes the analysis tools on top of
    it. It connects lazily on first use, so the pool is only opened if a query
    is actually run.

    Example:
        ```python
        import asyncio
        from pg_debug import AccessMode, PgDebug

        async def main():
            async with PgDebug("postgresql://localhost/mydb", access_mode=AccessMode.RESTRICTED) as pg:
                for schema in await pg.list_schemas():
                    print(schema["schema_name"])

        asyncio.run(main())
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
        """
        Args:
            database_url: PostgreSQL connection URL. Defaults to the
                ``DATABASE_URI`` or ``DATABASE_URL`` environment variable.
            access_mode: ``unrestricted`` (default) allows any statement;
                ``restricted`` allows only read-only statements.
            timeout: Statement timeout in seconds, applied in both access
                modes, from 0 (none) to 2147483, PostgreSQL's limit. Defaults
                to none in unrestricted mode and to
                ``DEFAULT_RESTRICTED_TIMEOUT`` in restricted mode.
            search_path: Schema search path for every statement, e.g.
                ``"reporting, public"``. Index tuning resolves table names
                through it. It and the timeout are set inside each statement's
                transaction, never for the session, so that behind a
                transaction pooler neither reaches another client.
            llm_model: Model for the ``llm`` index tuning method. Defaults to
                the ``PG_DEBUG_LLM_MODEL`` environment variable, then to
                :data:`~pg_debug.index.llm_settings.DEFAULT_LLM_MODEL`.
            pool: An existing connection pool to use. When given, the client
                does not close it on exit and does not reconfigure it, so
                ``search_path`` and the server-side timeout are its owner's
                responsibility.
            context: The role and settings to run your queries with, as your
                application's sessions would: a role that row-level security
                applies to, and the variables its policies read. Applied by
                :meth:`execute_sql`, :meth:`explain_query`,
                :meth:`diagnose_query`, :meth:`explain_index_use` and the
                plans index tuning compares, to each statement's transaction
                alone; pg-debug's own reads of the catalog run as the
                connecting user. Checked against the server when the client
                connects. Applies in restricted mode too.
        """
        check_statement_timeout(timeout)
        self._database_url = database_url
        self.access_mode = AccessMode(access_mode)
        self.timeout = timeout
        self.search_path = search_path
        self.llm_model = llm_model
        self.context = context
        self._context_checked = False
        self._platform: PlatformDetection | None = None
        self._pool = (
            pool
            if pool is not None
            else DbConnPool(
                search_path=search_path,
                statement_timeout=self.effective_timeout,
            )
        )
        self._owns_pool = pool is None

    @property
    def effective_timeout(self) -> int | None:
        """The statement timeout actually applied, in seconds.

        Restricted mode defaults to :data:`DEFAULT_RESTRICTED_TIMEOUT` because
        it is meant to be safe by default; unrestricted mode leaves queries
        unbounded unless a timeout is asked for.
        """
        if self.timeout is not None:
            return self.timeout
        return DEFAULT_RESTRICTED_TIMEOUT if self.access_mode == AccessMode.RESTRICTED else None

    @property
    def database_url(self) -> str:
        """The resolved connection URL."""
        return resolve_database_url(self._database_url)

    @property
    def pool(self) -> DbConnPool:
        """The underlying connection pool."""
        return self._pool

    async def connect(self) -> PgDebug:
        """Open the connection pool and verify the database is reachable.

        Calling this is optional; the client connects on first use. It is
        useful to fail fast on a bad URL.

        Raises:
            ConnectionFailedError: If the database could not be reached.
            InvalidArgumentError: If the server refused the query context: a
                role the connecting user is not a member of, or a setting it
                does not know or will not take.
        """
        url = self.database_url
        self._keep_timeout_off_a_server_that_refuses_it(url)
        try:
            await self._pool.pool_connect(url)
        except PgDebugError:
            raise
        except Exception as e:
            raise ConnectionFailedError(f"Could not connect to database: {obfuscate_password(str(e))}") from e
        await self._check_context()
        return self

    def _keep_timeout_off_a_server_that_refuses_it(self, url: str) -> None:
        """Keep the statement timeout on this side for a server that will not take statement_timeout.

        Aurora DSQL refuses ``statement_timeout`` at any scope, by its
        documentation; pg-debug has not been run against it. Set in each
        transaction's setup, as everywhere else, the timeout would make every
        statement fail before it ran, and restricted mode always has one. So
        on DSQL it is not set on the server. DSQL is recognised from the URL's
        host or its ``amzn-cluster-id`` option, before anything runs: no
        statement could run to ask the server.

        The pool keeps the same number of seconds on this side instead, which
        restricted mode does for every statement already. With ``timeout`` in
        unrestricted mode the bound is kept too, rather than the option
        refused: the option then means one thing in both modes, and a user who
        asked for a bound keeps the one that can be had instead of being left
        with none. It is weaker than the server's, and the warning says how.
        A pool handed in is its owner's to configure.
        """
        if not self._owns_pool or not self._pool.server_side_timeout:
            return
        detection = detect_from_url(url)
        if detection is None or not detection.platform.refuses_statement_timeout:
            return
        self._pool.server_side_timeout = False
        if self.effective_timeout:
            logger.warning(
                "%s refuses statement_timeout, so the %s-second timeout is not set on the server: pg-debug stops waiting for "
                "a statement after that long and asks the server to cancel it, which has not been tried there. pg-debug took "
                "the server for %s because %s.",
                detection.platform.label,
                self.effective_timeout,
                detection.platform.label,
                detection.signal,
            )

    async def _detect_platform(self) -> PlatformDetection:
        """Which managed platform the server runs on, detected once per client.

        One statement, which restricted mode allows, and the host and options
        of the URL the pool connected with;
        :func:`~pg_debug.platform.detect_platform` lists the signals.
        """
        if self._platform is None:
            driver = await self.sql_driver()
            self._platform = await detect_platform(driver, getattr(self._pool, "connection_url", None))
        return self._platform

    async def _require_hypopg(self, driver: SqlDriver | SafeSqlDriver) -> None:
        """Refuse what needs HypoPG where it is not installed, saying how to get it on this platform."""
        is_hypopg_installed, hypopg_message = await check_hypopg_installation_status(driver, platform=self._detect_platform)
        if not is_hypopg_installed:
            raise ExtensionRequiredError("hypopg", hypopg_message)

    async def _check_context(self) -> None:
        """Have the server refuse a bad context once, before any query depends on it."""
        if not self.context or self._context_checked:
            return
        await SqlDriver(conn=self._pool, context=self.context).check_context()
        self._context_checked = True

    async def close(self) -> None:
        """Close the connection pool, unless it was supplied by the caller."""
        if self._owns_pool:
            await self._pool.close()

    async def __aenter__(self) -> PgDebug:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def sql_driver(self) -> SqlDriver | SafeSqlDriver:
        """Return a SQL driver appropriate for the configured access mode.

        In restricted mode the driver rejects anything that is not read-only
        and applies the configured statement timeout.
        """
        if not self._pool.is_valid:
            await self.connect()
        else:
            # A pool handed in already open skips connect().
            await self._check_context()

        base_driver = SqlDriver(conn=self._pool, context=self.context)
        if self.access_mode == AccessMode.RESTRICTED:
            logger.debug("Using SafeSqlDriver with restrictions (RESTRICTED mode)")
            return SafeSqlDriver(sql_driver=base_driver, timeout=self.effective_timeout)

        logger.debug("Using unrestricted SqlDriver (UNRESTRICTED mode)")
        return base_driver

    # -- Schema introspection ------------------------------------------------

    async def list_schemas(self) -> list[dict[str, Any]]:
        """List all schemas in the database."""
        return await SchemaInspector(await self.sql_driver()).list_schemas()

    async def list_objects(self, schema_name: str, object_type: str | ObjectType = ObjectType.TABLE) -> list[dict[str, Any]]:
        """List objects of a given type (table, view, sequence, extension)."""
        return await SchemaInspector(await self.sql_driver()).list_objects(schema_name, object_type)

    async def get_object_details(
        self,
        schema_name: str,
        object_name: str,
        object_type: str | ObjectType = ObjectType.TABLE,
    ) -> dict[str, Any]:
        """Describe a single database object, including columns and indexes."""
        return await SchemaInspector(await self.sql_driver()).get_object_details(schema_name, object_name, object_type)

    # -- Query execution -----------------------------------------------------

    async def execute_sql(self, sql: str) -> list[dict[str, Any]]:
        """Run a SQL statement and return its rows.

        Args:
            sql: The statement to run. In restricted mode anything that is not
                read-only is rejected.

        Returns:
            One dict per row. Statements that produce no result set (DDL, for
            example) return an empty list.
        """
        driver = await self.sql_driver()
        rows = await driver.execute_query(sql, in_context=True)  # type: ignore[arg-type]
        return [row.cells for row in rows] if rows else []

    async def explain_query(
        self,
        sql: str,
        analyze: bool = False,
        hypothetical_indexes: list[dict[str, Any]] | None = None,
    ) -> ExplainPlanArtifact:
        """Return the execution plan for a query.

        Args:
            sql: The query to explain.
            analyze: Run the query and report real execution statistics rather
                than estimates. Slower, but more accurate.
            hypothetical_indexes: Indexes to simulate without creating them,
                each a dict with ``table``, ``columns`` and optionally ``using``
                (default ``btree``). Requires the HypoPG extension and cannot be
                combined with ``analyze``.

        Returns:
            The plan, whose ``to_text()`` renders a readable summary and whose
            ``plan_tree`` exposes the parsed nodes.

        Raises:
            InvalidArgumentError: If ``analyze`` and ``hypothetical_indexes``
                are combined, or an index definition is malformed.
            ExtensionRequiredError: If hypothetical indexes are requested but
                HypoPG is not installed.
            ExplainPlanError: If PostgreSQL could not plan the query.
        """
        driver = await self.sql_driver()
        explain_tool = ExplainPlanTool(sql_driver=driver)

        if hypothetical_indexes:
            if analyze:
                raise InvalidArgumentError("Cannot use analyze and hypothetical indexes together.")

            await self._require_hypopg(driver)
            return await explain_tool.explain_with_hypothetical_indexes(sql, hypothetical_indexes)
        if analyze:
            return await explain_tool.explain_analyze(sql)
        return await explain_tool.explain(sql)

    # -- Plan diagnosis ------------------------------------------------------

    async def diagnose_query(self, sql: str, min_ratio: float = DEFAULT_MIN_RATIO) -> list[PlanFinding]:
        """Run a query and say what is wrong with the plan it produced.

        Where :meth:`explain_query` hands back the plan, this reads it: the
        node whose row estimate first diverged from what it found, and whether
        that is stale statistics or two correlated columns the planner has no
        way to know are related; a sort or a hash that outgrew ``work_mem``; a
        scan reading rows in order to discard them; parallel workers asked for
        and not granted.

        Findings are ranked by the share of the query's work behind them, so a
        spill that is two percent of the runtime sorts below one that is most
        of it.

        This needs ``EXPLAIN ANALYZE``, which runs the query. A statement that
        writes is run inside a transaction that is rolled back — though a
        sequence it consumed is not given back, and a trigger that reached
        outside the database has already done so.

        Args:
            sql: The statement to diagnose. One statement, not a script.
            min_ratio: How far a row estimate has to be from the truth before
                it is reported, as a factor.

        Returns:
            :class:`~pg_debug.plans.PlanFinding` objects, most severe first. An
            empty list means the plan is a reasonable one.

        Raises:
            InvalidArgumentError: In restricted mode, which does not run
                queries, or if more than one statement is passed.
            ExplainPlanError: If the statement could not be run or its plan
                could not be read.
        """
        return await PlanDiagnoser(await self.sql_driver(), platform=self._detect_platform).diagnose(sql, min_ratio=min_ratio)

    async def diagnose_plan(self, plan: PlanInput, min_ratio: float = DEFAULT_MIN_RATIO) -> list[PlanFinding]:
        """Diagnose a plan that already exists, checking this database's catalog.

        The same reading as :meth:`diagnose_query`, over ``EXPLAIN (ANALYZE,
        FORMAT JSON)`` output produced elsewhere — ``auto_explain``, a replica,
        a colleague. Nothing is run, so this works in restricted mode. The
        tables the plan reads are checked here for stale statistics and for
        extended statistics covering their correlated columns, so the plan
        should come from this database or one with the same schema and data.
        :func:`pg_debug.diagnose_plan` does the same with no database at all.

        Args:
            plan: The EXPLAIN output as text or decoded JSON: the one-element
                list EXPLAIN prints, or the object inside it, which is what
                ``auto_explain`` logs.
            min_ratio: How far a row estimate has to be from the truth before
                it is reported, as a factor.

        Returns:
            :class:`~pg_debug.plans.PlanFinding` objects, most severe first.

        Raises:
            InvalidArgumentError: If the plan is not EXPLAIN JSON, or was
                produced without ANALYZE.
        """
        return await PlanDiagnoser(await self.sql_driver(), platform=self._detect_platform).diagnose_plan(plan, min_ratio=min_ratio)

    # -- Index use -----------------------------------------------------------

    async def explain_index_use(self, sql: str, index: str | None = None) -> list[IndexUse]:
        """Say whether the planner uses each index on the query's tables, and if not, why.

        The two halves of "why is my index not used" need different
        evidence, and both are gathered. Whether the planner *can* use it is
        read from the query's text against the index's definition and the
        catalog: the column each predicate restricts, bare or wrapped in an
        expression, the operator, an invalid index, a partial index the query
        does not match, a cast the plan applies to the column. Whether it
        *chose* to is read by planning the query again with sequential scans
        disabled: if the index is used then, nothing stopped it, and the
        finding reports both costs and the row estimate the choice rests on.

        Nothing is executed; the query is planned, not run, in restricted mode
        too: sequential scans are disabled for the forced plan's transaction
        alone.

        Args:
            sql: One SELECT, UPDATE or DELETE.
            index: Report only this index, by name.

        Returns:
            One :class:`~pg_debug.index_use.IndexUse` per index, the ones not
            used first, each with its findings.

        Raises:
            InvalidArgumentError: If the SQL does not parse, reads no indexed
                table, or names an index not on its tables.
            ExplainPlanError: If the query could not be planned.
        """
        return await IndexUseAnalyzer(await self.sql_driver()).analyze(sql, index=index)

    # -- Planner statistics --------------------------------------------------

    async def column_stats(self, table: str) -> list[dict[str, Any]]:
        """Report the planner's per-column statistics for a table.

        This is what to look at when ``explain --analyze`` shows estimated rows
        far from actual rows: the estimate comes from ``null_frac``,
        ``n_distinct`` and the most common values listed here. ``correlation``
        decides whether the planner expects an index scan to be cheap.

        Returns an empty list if the table has never been analyzed.
        """
        return await StatisticsInspector(await self.sql_driver()).column_stats(table)

    async def extended_statistics(self, table: str) -> list[dict[str, Any]]:
        """List the extended statistics objects defined on a table.

        Without them the planner treats columns as independent and
        underestimates rows for correlated multi-column predicates.
        """
        return await StatisticsInspector(await self.sql_driver()).extended_statistics(table)

    # -- Data model ----------------------------------------------------------

    async def review_data_model(
        self,
        rules: str | RuleId | Iterable[str | RuleId] = RuleId.ALL,
        schema: str | None = None,
        min_rows: int = DEFAULT_MIN_ROWS,
        proven_only: bool = False,
    ) -> list[ModelFinding]:
        """Review the data model for defects, and say how to fix each one.

        Where the other diagnostics ask what the server is doing, this asks
        whether the schema is built the way the data behaves: foreign keys with
        no index behind them, keys whose two sides are different types, tables
        with no primary key, columns ordered so that alignment padding costs
        bytes on every row, and types that do not mean what they appear to.

        Needs no extension, and every rule is one set-based query over the
        catalog, so it is cheap enough to run against a production database.

        Args:
            rules: Which rules to run: a :class:`RuleId`, a comma-separated
                string, or an iterable. ``all`` by default.
            schema: Restrict to one schema. Every user schema by default.
            min_rows: Rules that reason from sampled statistics ignore tables
                smaller than this.
            proven_only: Keep only what the catalog proves, dropping the
                findings inferred from statistics.

        Returns:
            :class:`~pg_debug.model.ModelFinding` objects, most severe first,
            each carrying the evidence behind it and the statements that fix
            it. A finding whose ``confidence`` is ``candidate`` was inferred
            from a sample and carries a ``verify`` query to settle it first.

        Raises:
            InvalidArgumentError: If an unknown rule is requested.
        """
        reviewer = ModelReviewer(await self.sql_driver())
        return await reviewer.review(rules=rules, schema=schema, min_rows=min_rows, proven_only=proven_only)

    async def check_migration(self, sql: str) -> list[StatementReview]:
        """Review a migration before running it.

        Reports, for each statement, the lock it takes, how long it holds it,
        and whether it rewrites the table. The lock level alone decides little:
        ACCESS EXCLUSIVE held for a catalog update lasts microseconds, while the
        same lock held for a rewrite of a large table is an outage. The
        connection is used to say which of those a statement actually is, by
        looking up the real size of every table it touches.

        Args:
            sql: The migration. Multiple statements are fine.

        Returns:
            :class:`~pg_debug.migration.StatementReview` objects, most severe
            first, each carrying a safer rewrite where one exists.

        Raises:
            InvalidArgumentError: If the SQL cannot be parsed.
        """
        return await MigrationLinter(await self.sql_driver()).review(sql)

    async def review_configuration(self) -> list[ConfigFinding]:
        """Review the server's settings against what it is actually doing.

        Every finding is argued from a counter this server is already keeping —
        blocks read against blocks hit, bytes spilled to temporary files,
        checkpoints forced by WAL volume rather than by the timer — so a
        default that is doing no harm is left alone. Nothing here guesses at
        the machine's memory or storage, neither of which PostgreSQL exposes.

        Returns:
            :class:`~pg_debug.configuration.ConfigFinding` objects, most severe
            first. Empty means nothing measurable is wrong, which is not the
            same as optimal.
        """
        reviewer = ConfigurationReviewer(await self.sql_driver(), platform=await self._detect_platform())
        return await reviewer.review()

    # -- Audits --------------------------------------------------------------

    async def check_upgrade(self, to: int | None = None) -> AuditReport:
        """Say what stands between this cluster and ``pg_upgrade`` to a newer major version.

        Reads, from the connection, what ``pg_upgrade`` refuses (``reg*``
        columns, prepared transactions, a checksum setting the new cluster
        will not share), what it carries across incomplete (logical slots and
        subscriptions before 17, extended statistics), and what is better
        done before (extension updates, MD5 passwords). Read-only, so it
        runs in restricted mode.

        Args:
            to: The major version to upgrade to. Defaults to the newest one
                pg-debug knows was released.

        Returns:
            An :class:`~pg_debug.audit.AuditReport`: the findings, most severe
            first, and what each check examined or why it could not tell.

        Raises:
            InvalidArgumentError: If ``to`` is not newer than the server.
        """
        checker = UpgradeChecker(await self.sql_driver(), platform=await self._detect_platform())
        return await checker.check(to=to)

    async def audit_security(self) -> AuditReport:
        """Read the server's authentication rules, roles, privileges and row-level security for what lets too much in.

        The authentication rules are readable by a superuser only; for any
        other role that check reports that it could not tell rather than
        passing. Read-only, so it runs in restricted mode.

        Returns:
            An :class:`~pg_debug.audit.AuditReport`: the findings, most severe
            first, and what each check examined or why it could not tell.
        """
        auditor = SecurityAuditor(await self.sql_driver(), platform=await self._detect_platform())
        return await auditor.audit()

    # -- Partitioning --------------------------------------------------------

    async def suggest_partitions(
        self,
        sql: str | list[str] | None = None,
        min_table_bytes: int = DEFAULT_MIN_TABLE_BYTES,
        min_pruning_share: float = DEFAULT_MIN_PRUNING_SHARE,
        limit: int = 200,
    ) -> list[PartitionCandidate]:
        """Decide whether a table is worth partitioning, from what the workload does.

        The number that decides it is not the table's size but the share of
        recorded query time that restricts on a candidate key, because those are
        the queries that would still read one partition instead of all of them.
        A verdict is returned for every table examined, including the ones this
        advises against: "31% of the time against this table would prune" is a
        more useful answer than silence, and it is the answer more often.

        Args:
            sql: Queries to judge by. Defaults to the workload recorded in
                ``pg_stat_statements``, weighted by total execution time.
            min_table_bytes: Ignore tables smaller than this. Below a gigabyte
                the complexity costs more than the pruning returns.
            min_pruning_share: The share of query time that has to keep pruning
                for a table to be recommended.
            limit: How many recorded queries to read.

        Returns:
            :class:`~pg_debug.partition.PartitionCandidate` objects, the
            recommended ones first and then by size. Each carries the share it
            was judged on, the unique constraints that would have to change, and
            for a recommended table the shape of the target — not a migration,
            which is a separate operation with its own risks.
        """
        queries = [sql] if isinstance(sql, str) else sql
        advisor = PartitionAdvisor(
            await self.sql_driver(),
            min_table_bytes=min_table_bytes,
            min_pruning_share=min_pruning_share,
        )
        return await advisor.analyze(query_list=queries, limit=limit)

    # -- Scale-out and kept history ------------------------------------------

    async def assess_shard_readiness(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> ShardReadiness:
        """Say whether to shard, which usually means "not yet", with the figure behind each reason.

        Samples the write path for ``interval_seconds`` (the WAL written, the
        shared buffers evicted from PostgreSQL 16, the checkpoints), reads the
        cumulative checkpoint counts and the autovacuum backlog, and weighs
        the workload ``pg_stat_statements`` recorded: the share of time in
        reads, in the top five statements, and on the best candidate routing
        key. Each "do not shard" comes back as a
        :class:`~pg_debug.sharding.Refusal` with its figure. On a Citus
        coordinator it also reads the cluster's own views. Everything it runs
        is a read, so it works in restricted mode.

        Args:
            interval_seconds: How long to sample. The figures describe that
                interval, so a short one describes little; it is reported with
                them.

        Returns:
            A :class:`~pg_debug.sharding.ShardReadiness`: the verdict
            (``not yet`` by default, ``worth evaluating`` only when nothing
            refuses and the write path is under pressure), the refusals, the
            pressure, what would change the verdict, the candidate routing
            keys and the measurements.

        Raises:
            InvalidArgumentError: If the interval is not positive.
        """
        return await ShardReadinessAssessor(await self.sql_driver()).assess(interval_seconds)

    async def read_history(self, limit: int = 10) -> HistoryReport:
        """Read the history the platform and the installed extensions keep, and how far back each goes.

        Azure's Query Store, Aurora's ``aurora_stat_statements()`` and
        ``aurora_stat_plans()``, the Cloud SQL and AlloyDB index advisor's
        recommendations, and Neon's local file cache, chosen by the detected
        platform or by the object being in this database; and, wherever they
        are installed, ``pg_qualstats`` and ``pg_wait_sampling``. A source
        whose capture is off says so and with which setting. The platform
        readers follow each platform's documentation and have not been run on
        the platforms.

        Args:
            limit: How many rows to read from each source.

        Returns:
            A :class:`~pg_debug.history.HistoryReport`, one
            :class:`~pg_debug.history.HistorySource` per source examined.

        Raises:
            InvalidArgumentError: If the limit is below 1.
        """
        return await HistoryReader(await self.sql_driver(), await self._detect_platform()).read(limit=limit)

    # -- Live activity -------------------------------------------------------

    async def list_activity(
        self,
        include_idle: bool = False,
        min_duration_seconds: float | None = None,
        blocked_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Report what the server is doing right now.

        Args:
            include_idle: Include idle client backends and idle auxiliary
                processes, which are hidden by default.
            min_duration_seconds: Only report backends that have held their
                current state at least this long.
            blocked_only: Only report backends waiting on a lock another
                backend holds. Implies ``include_idle``, since a blocked
                backend can be idle in transaction.

        Returns:
            One dict per backend, including ``blocked_by`` (the pids blocking
            it), ``in_transaction``, and the age of its state, query and
            transaction. A role without ``pg_read_all_stats`` is shown other
            roles' backends without their state, query, wait or timings: the
            ones in a transaction are listed with the running ones, and
            :meth:`statistics_access` and :meth:`withheld_backends` say what
            was withheld.
        """
        inspector = ActivityInspector(await self.sql_driver())
        if blocked_only:
            return await inspector.blocked_backends()
        return await inspector.list_backends(include_idle=include_idle, min_duration_seconds=min_duration_seconds)

    async def vacuum_progress(self) -> list[dict[str, Any]]:
        """Report running vacuums and how far along they are."""
        return await ActivityInspector(await self.sql_driver()).vacuum_progress()

    async def lock_tree(self) -> list[dict[str, Any]]:
        """Report who is blocking whom, as trees rooted at the backends that wait on nothing.

        Returns:
            One dict per root blocker, the one blocking the most backends
            first, with the backends waiting on it nested under ``blocking``
            and what each waits for under ``waits_for``. See
            :meth:`ActivityInspector.lock_tree`.
        """
        return await ActivityInspector(await self.sql_driver()).lock_tree()

    async def sample_waits(self, seconds: float, interval: float = DEFAULT_SAMPLE_INTERVAL) -> WaitProfile:
        """Sample what active sessions wait on, without an extension.

        Reads pg_stat_activity every ``interval`` seconds for ``seconds``, one
        statement per sample, leaving out pg-debug's own backend and
        statements, and builds a profile: average active sessions overall,
        per wait event and per query. Where pg_wait_sampling is installed its
        profile over the same window is read too; where Azure's Query Store
        records waits, its most recent interval.

        Args:
            seconds: How long to sample.
            interval: Seconds between samples (default 0.5).

        Returns:
            A :class:`~pg_debug.waits.WaitProfile` with the number of samples
            taken and the interval achieved, measured on the server's clock.
            Each sample is exact; the profile is a sample, so its
            attributions are candidates.

        Raises:
            InvalidArgumentError: If the duration or interval is not one a
                sample can be taken with.
        """
        return await WaitSampler(await self.sql_driver()).sample(seconds, interval=interval)

    async def classify_waits(self) -> WaitDiagnosis:
        """Name the patterns in one snapshot of the server's waits.

        A lock queue behind DDL, an idle-in-transaction session blocking
        others, row lock contention, and LWLock pressure on BufferMapping or
        LockManager, each with the pids, lock modes and relation it was read
        from, and a confidence.

        Returns:
            A :class:`~pg_debug.waits.WaitDiagnosis`.
        """
        return await classify_waits(await self.sql_driver())

    # -- Diagnostics ---------------------------------------------------------

    async def analyze_db_health(
        self,
        health_type: str | HealthType | Iterable[str | HealthType] = HealthType.ALL,
        min_window_days: float = MIN_WINDOW_DAYS,
    ) -> dict[str, HealthCheckResult]:
        """Run database health checks.

        Each check runs on its own: one the server refuses, or a statement
        timeout cancels behind a lock, is returned with ``error`` set rather
        than raised, and the others still run.

        Args:
            health_type: Which checks to run: ``index``, ``connection``,
                ``vacuum``, ``sequence``, ``replication``, ``buffer``,
                ``constraint``, ``statistics``, ``table``, ``scan`` or
                ``all`` (default). Accepts a comma-separated string or an
                iterable.
            min_window_days: How many days the index counters must have
                been counting before an index they show unscanned is called
                unused (default 7). A crash, a failover, a restore or
                ``pg_stat_reset()`` starts them again; with less than this,
                the unused-index check says why it did not judge.

        Returns:
            A mapping of check name to its :class:`HealthCheckResult`, each
            carrying a ``healthy`` flag, a text ``summary``, the flagged
            ``findings``, the ``metrics`` behind the verdict and, for a check
            that could not run, the ``error`` that stopped it.

        Raises:
            InvalidArgumentError: If an unknown health type is requested.
        """
        health_tool = DatabaseHealthTool(await self.sql_driver(), min_window_days=min_window_days)
        return await health_tool.run_checks(health_type)

    async def get_top_queries(
        self,
        sort_by: SortBy = "resources",
        limit: int = 10,
        interval_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        """Report the most expensive queries recorded by pg_stat_statements.

        Args:
            sort_by: ``resources`` (default) ranks by a blend of execution
                time, buffer access and WAL volume; ``mean_time`` and
                ``total_time`` rank by execution time.
            limit: Number of queries to return. Ranked by ``resources``, they
                are the queries over its threshold with the largest share of
                any one resource.
            interval_seconds: Sample over this many seconds instead of
                reporting cumulative totals. Cumulative counters cover
                everything since statistics were last reset, which is often
                dominated by a migration or bulk load rather than the live
                workload; sampling measures what is running now.

        Returns:
            One dict of statistics per query. A role without
            ``pg_read_all_stats`` is shown other roles' statements with
            ``<insufficient privilege>`` for their text; a sample leaves them
            out, and :meth:`sample_top_queries` says how many ran.

        Raises:
            InvalidArgumentError: If ``sort_by`` is not a valid criterion, or
                the interval is not positive.
            ExtensionRequiredError: If pg_stat_statements is not installed.
        """
        if interval_seconds is not None:
            return (await self.sample_top_queries(interval_seconds, sort_by=sort_by, limit=limit)).queries

        _check_sort_by(sort_by)
        top_queries_tool = TopQueriesCalc(sql_driver=await self.sql_driver())

        if sort_by == "resources":
            return await top_queries_tool.get_top_resource_queries(limit=limit)
        return await top_queries_tool.get_top_queries_by_time(limit=limit, sort_by="mean" if sort_by == "mean_time" else "total")

    async def sample_top_queries(
        self,
        interval_seconds: float,
        sort_by: SortBy = "resources",
        limit: int = 10,
    ) -> QuerySample:
        """Measure the most expensive queries over an interval, and say what the ranking left out.

        Takes the arguments :meth:`get_top_queries` takes with
        ``interval_seconds``, and ranks the same way.

        Returns:
            A :class:`~pg_debug.top_queries.QuerySample`: ``queries``, the
            ranking ``get_top_queries`` returns, and ``withheld``, how many of
            other roles' statements ran during the interval and are not in it.
            pg_stat_statements shows a role without ``pg_read_all_stats``
            their counters but not their queryid, so they cannot be followed
            from one snapshot to the next.

        Raises:
            InvalidArgumentError: If ``sort_by`` is not a valid criterion, or
                the interval is not positive.
            ExtensionRequiredError: If pg_stat_statements is not installed.
        """
        _check_sort_by(sort_by)
        sample_sort = {"resources": "resources", "mean_time": "mean", "total_time": "total"}[sort_by]
        top_queries_tool = TopQueriesCalc(sql_driver=await self.sql_driver())
        return await top_queries_tool.measure(interval_seconds, sort_by=sample_sort, limit=limit)  # type: ignore[arg-type]

    async def sample_server_rates(self, interval_seconds: float = DEFAULT_RATES_INTERVAL) -> ServerRates:
        """Measure the server's own counters over an interval, as rates per second.

        Reads ``pg_stat_database`` for this database, how far WAL was written
        (or replayed, on a standby), ``pg_stat_wal`` (PostgreSQL 14+), the
        checkpointer and background writer, ``pg_stat_io`` (16+) by process
        type and object, and the WAL each replication slot retains, twice,
        ``interval_seconds`` apart.

        Args:
            interval_seconds: How long to wait between the two snapshots
                (default 10). Backends report their counters about once a
                second, so a few seconds misses some of what ran at the edges.

        Returns:
            A :class:`~pg_debug.server_rates.ServerRates`: one
            :class:`~pg_debug.server_rates.RateGroup` per view row, each with
            the interval measured between its own two reads and every
            counter's delta and rate. A group whose counters were reset during
            the interval, or a counter that went backwards, carries
            ``refused`` with the reason instead of a rate; so does a view this
            version does not have.

        Raises:
            InvalidArgumentError: If the interval is not positive.
        """
        return await ServerRatesSampler(await self.sql_driver()).sample(interval_seconds)

    async def inspect_pooler(self, admin_url: str) -> PoolerReport:
        """Read PgBouncer's admin console and report whether clients wait for a server connection.

        A client waiting in the pool is not a backend yet, so the server
        cannot show it. The console can: ``SHOW POOLS`` (clients active and
        waiting, server connections active and idle, how long the oldest
        waiter has waited), ``SHOW STATS`` (average wait, transaction and
        query times), ``SHOW DATABASES`` and ``SHOW CONFIG`` (pool mode and
        sizes). It is reached on a connection of its own, sending it only
        those ``SHOW`` commands: see :mod:`pg_debug.pooler` for why not
        through the driver.

        When this client has a database URL, the server's
        ``max_connections`` is read through it too, after the console, to
        compare the pool sizes with; without one, the report says it was not.
        Give the server's own address when you can: through a full pool the
        read waits in line like every other client, up to PgBouncer's
        ``query_wait_timeout``, and the report then says it could not be read.

        Args:
            admin_url: The admin console,
                ``postgresql://user@host:6432/pgbouncer``, as a user in
                ``admin_users`` or ``stats_users``.

        Returns:
            A :class:`~pg_debug.pooler.PoolerReport`: what the console
            showed, and the ``findings`` of saturation, the most severe
            first.

        Raises:
            InvalidArgumentError: If the URL is not the admin console's.
            ConnectionFailedError: If the console could not be reached.
        """
        check_admin_url(admin_url)
        try:
            self.database_url  # noqa: B018 - raises when there is none
        except InvalidArgumentError:
            reason = "no database URL was given, so the pool sizes are not compared with the server's max_connections"
            return await inspect_pgbouncer(admin_url, server_refused=reason)

        async def read_server() -> dict[str, Any]:
            return await read_server_connections(await self.sql_driver())

        return await inspect_pgbouncer(admin_url, read_server=read_server)

    # -- What the statistics views withhold ----------------------------------

    async def statistics_access(self) -> StatisticsAccess:
        """Whether the connecting role is shown every role's activity and statements.

        Without ``pg_read_all_stats``, which ``pg_monitor`` includes,
        PostgreSQL shows a role other roles' backends without their state,
        query, wait or timings, and their statements without their text.
        ``list_activity``, ``lock_tree``, ``vacuum_progress`` and
        ``get_top_queries`` return what it showed.

        Returns:
            A :class:`~pg_debug.db.privileges.StatisticsAccess`: the role,
            whether it is shown everything, whether it is a member of
            ``pg_read_all_stats`` that does not inherit its privileges, and
            the statement that would show it everything.
        """
        return await privileges.statistics_access(await self.sql_driver())

    async def withheld_backends(self) -> list[dict[str, Any]]:
        """The backends whose details PostgreSQL withholds from the connecting role, and what it still shows of them.

        Returns:
            One dict per backend, with its pid, database, user and
            application, whether it is in a transaction, how many transactions
            old its snapshot is, the locks it holds, and the pids waiting on
            it. Empty for a role shown everything. See
            :func:`pg_debug.db.privileges.withheld_backends`.
        """
        return await privileges.withheld_backends(await self.sql_driver())

    async def counting_windows(self) -> dict[str, dict[str, Any] | None]:
        """How long each set of counters pg-debug argues from has been counting.

        A counter is a count since it last started from zero: an index
        unscanned for a year and one unscanned since a crash an hour ago look
        the same in it. A clean restart does not start them again; a crash,
        an immediate shutdown, a failover, a restore or a reset does. See
        :mod:`pg_debug.db.window`.

        Returns:
            ``tables`` for this database's table and index counters,
            ``checkpoints`` for the checkpoint counters, and ``statements``
            for pg_stat_statements (None when it is not installed here), each
            with ``counting_since``, ``counting_seconds`` and
            ``counting_began_with`` (``reset``, ``restart`` or None when
            PostgreSQL did not record it). ``statements`` also has
            ``statements_evicted`` from pg_stat_statements 1.9 on: entries
            evicted for new ones, which counted for less than the window.
        """
        driver = await self.sql_driver()
        stats = await locate_pg_stat_statements(driver)
        return {
            "tables": (await table_window(driver)).to_dict(),
            "checkpoints": (await checkpoint_window(driver)).to_dict(),
            "statements": (await statement_window(driver, stats)).to_dict() if stats else None,
        }

    # -- Index tuning --------------------------------------------------------

    async def analyze_workload_indexes(
        self,
        max_index_size_mb: int = 10000,
        method: TuningMethod = "dta",
        llm_model: str | None = None,
    ) -> dict[str, Any]:
        """Recommend indexes for the workload recorded by pg_stat_statements.

        Args:
            max_index_size_mb: Total size budget for recommended indexes.
            method: ``dta`` (default) searches index combinations against the
                planner; ``llm`` asks a language model for candidates and
                requires the ``llm`` extra.
            llm_model: Model to use for the ``llm`` method, overriding the
                client's own setting for this call.

        Returns:
            A dict with ``summary``, ``recommendations`` and ``query_impact``.
            When nothing is worth recommending, ``recommendations`` says so.

        Raises:
            InvalidArgumentError: If ``method`` is not ``dta`` or ``llm``.
            ExtensionRequiredError: If HypoPG is not installed.
            IndexTuningError: If the tables have never been analyzed, none of
                the workload could be analyzed, or the analysis failed.
        """
        presentation = await self._index_tuning_presentation(method, llm_model)
        return await presentation.analyze_workload(max_index_size_mb=max_index_size_mb)

    async def analyze_query_indexes(
        self,
        queries: list[str],
        max_index_size_mb: int = 10000,
        method: TuningMethod = "dta",
        llm_model: str | None = None,
    ) -> dict[str, Any]:
        """Recommend indexes for a specific list of queries.

        Args:
            queries: The queries to tune for, at most
                ``MAX_NUM_INDEX_TUNING_QUERIES``.
            max_index_size_mb: Total size budget for recommended indexes.
            method: ``dta`` (default) or ``llm``.
            llm_model: Model to use for the ``llm`` method, overriding the
                client's own setting for this call.

        Returns:
            A dict with ``summary``, ``recommendations`` and ``query_impact``.
            When nothing is worth recommending, ``recommendations`` says so.

        Raises:
            InvalidArgumentError: If no queries are given, or too many.
            ExtensionRequiredError: If HypoPG is not installed.
            IndexTuningError: If the tables have never been analyzed, none of
                the queries could be analyzed, or the analysis failed.
        """
        if not queries:
            raise InvalidArgumentError("Please provide a non-empty list of queries to analyze.")
        if len(queries) > MAX_NUM_INDEX_TUNING_QUERIES:
            raise InvalidArgumentError(f"Please provide a list of up to {MAX_NUM_INDEX_TUNING_QUERIES} queries to analyze.")
        if method == "llm" and len(queries) > 1:
            # The LLM optimizer reasons about one plan at a time; say so before
            # opening a connection rather than failing deep in the analysis.
            raise InvalidArgumentError("The 'llm' method analyzes one query at a time. Pass a single query, or use the 'dta' method.")

        presentation = await self._index_tuning_presentation(method, llm_model)
        return await presentation.analyze_queries(queries=queries, max_index_size_mb=max_index_size_mb)

    async def _index_tuning_presentation(self, method: TuningMethod, llm_model: str | None = None) -> TextPresentation:
        if method not in ("dta", "llm"):
            raise InvalidArgumentError(f"Unknown index tuning method: {method}. Valid values are: dta, llm.")

        driver = await self.sql_driver()
        advisor: IndexTuningBase
        if method == "dta":
            advisor = DatabaseTuningAdvisor(driver)
        else:
            # Here rather than at the top: the method needs the llm extra, and
            # importing it without the extra raises MissingDependencyError.
            from .index.llm import LLMOptimizerTool

            advisor = LLMOptimizerTool(driver, model=llm_model or self.llm_model)

        # The advisor checks for HypoPG as well, but it does not know the
        # platform, which decides how to get HypoPG where it is missing.
        await self._require_hypopg(driver)
        return TextPresentation(driver, advisor)


__all__ = [
    "DATABASE_URL_ENV_VARS",
    "AccessMode",
    "PgDebug",
    "resolve_database_url",
]
