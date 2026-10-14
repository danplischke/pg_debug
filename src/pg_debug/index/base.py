import json
import logging
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import LiteralString
from typing import cast

from pglast import parse_sql
from pglast.ast import SelectStmt
from pglast.parser import ParseError

from ..db import QUERY_MARKER
from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..db import check_hypopg_installation_status
from ..db import split_table_reference
from ..db import table_identifier
from ..db.statements import PG_STAT_STATEMENTS
from ..db.statements import THIS_DATABASE
from ..db.statements import locate_pg_stat_statements
from ..errors import ExtensionRequiredError
from ..errors import IndexTuningError
from ..errors import InvalidArgumentError
from ..errors import PgDebugError
from ..parsing import SqlBindParams
from ..parsing import TableAliasVisitor
from ..plans import ExplainPlanTool
from ..plans.hypothetical import IndexDefinition
from ..plans.tree import calculate_improvement_multiple

logger = logging.getLogger(__name__)

MAX_NUM_INDEX_TUNING_QUERIES = 10

#: What pg_stat_statements shows instead of another role's query text, unless
#: the reading role is a superuser or has been granted pg_read_all_stats.
HIDDEN_QUERY_TEXT = "<insufficient privilege>"

#: How heavily index maintenance weighs in the search's objective.
#:
#: Deliberately small, and calibrated rather than assumed. The term is
#: beta * log(write work), where write work counts each table's writes once for
#: the heap and once more for every index on it — so adding the first index to
#: a table moves it by log(2), about 0.69, whatever the write volume, while a
#: worthwhile index typically improves the time term by 0.2 to 3. At 1.0 the
#: term therefore vetoes indexes that are plainly worth having, which is what
#: it did when first tried. At 0.1 it breaks ties — between two candidates of
#: similar benefit the one on the quieter table wins — without overruling a
#: real improvement. Raise it for a service where writes dominate; 0 restores
#: the previous behaviour of ignoring maintenance entirely.
DEFAULT_WRITE_WEIGHT = 0.1

#: Relation kinds an index can be created on: ordinary tables, materialized
#: views and partitioned tables. A view is not one of them — it has no storage
#: of its own, and the index belongs on the tables underneath it.
INDEXABLE_RELKINDS = ("r", "m", "p")


def pp_list(lst: list[Any]) -> str:
    """Pretty print a list for debugging."""
    return ("\n  - " if len(lst) > 0 else "") + "\n  - ".join([str(item) for item in lst])


@dataclass
class IndexRecommendation:
    """Represents a database index with size estimation and definition."""

    _definition: IndexDefinition
    estimated_size_bytes: int = 0
    potential_problematic_reason: str | None = None

    def __init__(
        self,
        table: str,
        columns: tuple[str, ...],
        using: str = "btree",
        estimated_size_bytes: int = 0,
        potential_problematic_reason: str | None = None,
    ):
        self._definition = IndexDefinition(table, columns, using)
        self.estimated_size_bytes = estimated_size_bytes
        self.potential_problematic_reason = potential_problematic_reason

    @property
    def index_definition(self) -> IndexDefinition:
        return self._definition

    @property
    def definition(self) -> str:
        return self._definition.definition

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def columns(self) -> tuple[str, ...]:
        return self._definition.columns

    @property
    def table(self) -> str:
        return self._definition.table

    @property
    def using(self) -> str:
        return self._definition.using

    def __hash__(self) -> int:
        return self._definition.__hash__()

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, IndexRecommendation):
            return NotImplemented
        # Size is a measurement of the index, not part of its identity.
        return self._definition == other.index_definition

    def __str__(self) -> str:
        return self._definition.__str__() + f" (estimated_size_bytes: {self.estimated_size_bytes})"

    def __repr__(self) -> str:
        return self._definition.__repr__() + f" (estimated_size_bytes: {self.estimated_size_bytes})"


@dataclass
class IndexRecommendationAnalysis:
    """Represents a recommended index with benefit estimation."""

    index_recommendation: IndexRecommendation

    progressive_base_cost: float
    progressive_recommendation_cost: float
    individual_base_cost: float
    individual_recommendation_cost: float
    queries: list[str]
    definition: str
    #: What this index will be maintained for: the writes on its table that
    #: touch every index, and the share of updates currently avoiding them.
    write_load: dict[str, float] = field(default_factory=dict)

    @property
    def table(self) -> str:
        return self.index_recommendation.table

    @property
    def columns(self) -> tuple[str, ...]:
        return self.index_recommendation.columns

    @property
    def using(self) -> str:
        return self.index_recommendation.using

    @property
    def progressive_improvement_multiple(self) -> float:
        """Calculate the progressive percentage improvement from this recommendation."""
        return calculate_improvement_multiple(self.progressive_base_cost, self.progressive_recommendation_cost)

    @property
    def potential_problematic_reason(self) -> str | None:
        return self.index_recommendation.potential_problematic_reason

    @property
    def estimated_size_bytes(self) -> int:
        return self.index_recommendation.estimated_size_bytes

    @property
    def individual_improvement_multiple(self) -> float:
        """Calculate the individual percentage improvement from this recommendation."""
        return calculate_improvement_multiple(self.individual_base_cost, self.individual_recommendation_cost)

    def to_index(self) -> IndexRecommendation:
        return self.index_recommendation


@dataclass
class IndexTuningResult:
    """Results of index tuning analysis."""

    # Session ID for tracing
    session_id: str

    # Input parameters
    budget_mb: int  # Tuning budget in MB
    workload_source: str = "n/a"  # 'args', 'query_list', 'query_store', 'sql_file'
    workload: list[dict[str, Any]] | None = None

    # Output results
    recommendations: list[IndexRecommendationAnalysis] = field(default_factory=list)
    dta_traces: list[str] = field(default_factory=list)


def candidate_str(indexes: Iterable[IndexDefinition] | Iterable[IndexRecommendation] | Iterable[IndexRecommendationAnalysis]) -> str:
    return ", ".join(f"{idx.table}({','.join(idx.columns)})" for idx in indexes) if indexes else "(no indexes)"


class IndexTuningBase(ABC):
    def __init__(
        self,
        sql_driver: SqlDriver,
    ):
        """
        :param sql_driver: Database access
        """
        self.sql_driver = sql_driver

        # Add memoization caches
        self.cost_cache: dict[frozenset[IndexDefinition], float] = {}
        self._size_estimate_cache: dict[tuple[str, frozenset[str]], int] = {}
        # Sizes HypoPG actually measured, which beat the fallback formula.
        self._measured_size_cache: dict[tuple[str, frozenset[str]], int] = {}
        self._table_size_cache = {}
        # Index-touching writes per table, from pg_stat_user_tables.
        self._write_load_cache: dict[str, dict[str, float]] = {}
        self._estimate_table_size_cache = {}
        self._explain_plans_cache = {}
        self._sql_bind_params = SqlBindParams(self.sql_driver)

        # Add trace accumulator
        self._dta_traces: list[str] = []

        # Set when part of the workload could not be read; see
        # _describe_dropped_queries.
        self._workload_warning: str | None = None

    async def analyze_workload(
        self,
        workload: list[dict[str, Any]] | None = None,
        sql_file: str | None = None,
        query_list: list[str] | None = None,
        min_calls: int = 50,
        min_avg_time_ms: float = 5.0,
        limit: int = MAX_NUM_INDEX_TUNING_QUERIES,
        max_index_size_mb: int = -1,
    ) -> IndexTuningResult:
        """
        Analyze query workload and recommend indexes.

        This method can analyze workload from three different sources (in order of priority):
        1. Explicit workload passed as a parameter
        2. Direct list of SQL queries passed as query_list
        3. SQL file with queries
        4. Query statistics from pg_stat_statements

        Args:
            workload: Optional explicit workload data
            sql_file: Optional path to a file containing SQL queries
            query_list: Optional list of SQL query strings to analyze
            min_calls: Minimum number of calls for a query to be considered (for pg_stat_statements)
            min_avg_time_ms: Minimum average execution time in ms (for pg_stat_statements)
            limit: Maximum number of queries to analyze (for pg_stat_statements)
            max_index_size_mb: Maximum total size of recommended indexes in MB

        Returns:
            IndexTuningResult with analysis results. A workload with nothing to
            recommend returns one with no recommendations.

        Raises:
            ExtensionRequiredError: If HypoPG is not installed.
            IndexTuningError: If the tables have never been analyzed, none of
                the workload's queries could be analyzed, or the analysis
                failed part way.
            PgDebugError: Any other error from the library, such as a
                QueryError from the database, unchanged.
        """
        session_id = str(int(time.time()))
        self._analysis_start_time = time.time()
        self._dta_traces = []  # Reset traces at start of analysis

        # Clear the caches at the beginning of each analysis
        self._size_estimate_cache = {}
        self._measured_size_cache = {}

        if max_index_size_mb > 0:
            self.budget_mb = max_index_size_mb

        session = IndexTuningResult(
            session_id=session_id,
            budget_mb=max_index_size_mb,
        )

        try:
            # One connection for the whole analysis, which saves borrowing one
            # from the pool for each of its many statements. Nothing relies on
            # it reaching one backend: each batch that creates hypothetical
            # indexes plans with them and resets them in one transaction.
            async with self.sql_driver.session():
                await self._run_prechecks()

                # First try to use explicit workload if provided
                if workload:
                    logger.debug("Using explicit workload with %s queries", len(workload))
                    session.workload_source = "args"
                    session.workload = workload
                # Then try direct query list if provided
                elif query_list:
                    logger.debug("Using provided query list with %s queries", len(query_list))
                    session.workload_source = "query_list"
                    session.workload = []
                    for i, query in enumerate(query_list):
                        # Create a synthetic workload entry for each query
                        session.workload.append(
                            {
                                "query": query,
                                "queryid": f"direct-{i}",
                            }
                        )

                # Then try SQL file if provided
                elif sql_file:
                    logger.debug("Reading queries from file: %s", sql_file)
                    session.workload_source = "sql_file"
                    session.workload = self._get_workload_from_file(sql_file)

                # Finally fall back to query stats
                else:
                    logger.debug("Using query statistics from the database")
                    session.workload_source = "query_store"
                    session.workload = await self._get_query_stats(min_calls, min_avg_time_ms, limit)

                if not session.workload:
                    logger.warning("No workload to analyze")
                    return session

                session.workload = await self._validate_and_parse_workload(session.workload)

                query_weights = self._covert_workload_to_query_weights(session.workload)

                if query_weights is None or len(query_weights) == 0:
                    self.dta_trace("No query provided")
                    # "No index recommendations" reads as "your indexes are
                    # fine". If the workload was dropped rather than analyzed,
                    # say which it was.
                    if self._workload_warning:
                        raise IndexTuningError(self._workload_warning)
                    session.recommendations = []
                else:
                    # Gather queries as strings
                    workload_queries = [q for q, _, _ in query_weights]

                    self.dta_trace(f"Workload queries ({len(workload_queries)}): {pp_list(workload_queries)}")

                    # Generate and evaluate index recommendations
                    recommendations: tuple[set[IndexRecommendation], float] = await self._generate_recommendations(query_weights)
                    session.recommendations = await self._format_recommendations(query_weights, recommendations)

        except PgDebugError:
            raise
        except Exception as e:
            logger.error("Error in workload analysis: %s", e, exc_info=True)
            raise IndexTuningError(f"Index tuning failed: {e}") from e

        session.dta_traces = self._dta_traces
        return session

    async def _run_prechecks(self) -> None:
        """Refuse to analyze what the advisor cannot plan against.

        Raises:
            ExtensionRequiredError: If HypoPG is not installed.
            IndexTuningError: If no table has ever been analyzed.
        """
        # Pre-check 1: Check HypoPG with more granular feedback
        is_hypopg_installed, hypopg_message = await check_hypopg_installation_status(self.sql_driver)
        if not is_hypopg_installed:
            raise ExtensionRequiredError("hypopg", hypopg_message)

        # Pre-check 2: Check if ANALYZE has been run at least once
        result = await self.sql_driver.execute_query("SELECT s.last_analyze FROM pg_stat_user_tables s ORDER BY s.last_analyze LIMIT 1;")
        if not result or not any(row.cells.get("last_analyze") is not None for row in result):
            raise IndexTuningError(
                "Statistics are not up-to-date. The database needs to be analyzed first. "
                "Please run 'ANALYZE;' on your database before using the tuning advisor. "
                "Without up-to-date statistics, the index recommendations may be inaccurate."
            )

    async def _validate_and_parse_workload(self, workload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the queries that can be analyzed, and say what was dropped.

        A workload read from ``pg_stat_statements`` is not all valid SQL. A role
        without ``pg_read_all_stats`` sees ``<insufficient privilege>`` in place
        of other roles' query text, and a statement can use grammar the bundled
        parser does not know. Neither is a reason to abandon the rest of the
        workload, which is what an uncaught parse error here used to do.
        """
        validated_workload = []
        dropped: dict[str, int] = {}
        plan_error: str | None = None

        for q in workload:
            query_text = q["query"]
            if not query_text:
                logger.debug("Skipping empty query")
                continue
            query_text = query_text.strip().lower()

            if QUERY_MARKER in query_text:
                # pg_debug's own catalog queries are not the user's workload.
                logger.debug("Skipping one of our own queries")
                continue

            if query_text.startswith(HIDDEN_QUERY_TEXT):
                dropped["hidden"] = dropped.get("hidden", 0) + 1
                continue

            # Replace parameter placeholders with dummy values
            query_text = await self._sql_bind_params.replace_parameters(query_text)

            try:
                parsed = parse_sql(query_text)
            except ParseError as e:
                # One malformed entry among hundreds is not a reason to report
                # nothing at all.
                dropped["unparseable"] = dropped.get("unparseable", 0) + 1
                logger.debug("Skipping unparseable query (%s): %s...", e, query_text[:50])
                continue
            if not parsed:
                logger.debug("Skipping non-parseable query: %s...", query_text[:50])
                continue
            stmt = parsed[0].stmt
            if not self._is_analyzable_stmt(stmt):
                logger.debug("Skipping non-analyzable query: %s...", query_text[:50])
                continue

            error = await self._plan_error(query_text)
            if error is not None:
                dropped["unplannable"] = dropped.get("unplannable", 0) + 1
                plan_error = plan_error or error
                continue

            q["query"] = query_text
            q["stmt"] = stmt
            validated_workload.append(q)

        self._workload_warning = self._describe_dropped_queries(dropped, kept=len(validated_workload), plan_error=plan_error)
        if self._workload_warning:
            logger.warning(self._workload_warning)
        return validated_workload

    async def _plan_error(self, query_text: str) -> str | None:
        """Why the server will not plan the query as reconstructed, or None if it will.

        pg_stat_statements records queries with their constants replaced by
        placeholders, and the values put back in their place are guesses. A
        guess of the wrong type — an integer where the column is an enum or a
        domain — plans as an error. So does a query on a table the role
        planning it may not read. Finding that out here costs nothing, since
        the plan is cached and the analysis needs it anyway; finding it out
        later aborted the whole run.
        """
        try:
            await self.get_explain_plan_with_indexes(query_text, frozenset())
            return None
        except Exception as e:
            logger.debug("Skipping query the server will not plan (%s): %s...", e, query_text[:80])
            message = str(e).strip()
            return message.splitlines()[0] if message else type(e).__name__

    @staticmethod
    def _describe_dropped_queries(dropped: dict[str, int], kept: int, plan_error: str | None = None) -> str | None:
        """Explain a workload that was read only in part, or not at all.

        Silence here would be misleading: an empty workload reports "no index
        recommendations", which reads as "your indexes are fine" rather than
        "none of your queries could be used". A query that could not be
        planned is reported with what the server said about the first, since
        the cause is not always the one this can guess.
        """
        first = f" (the first: {plan_error})" if plan_error else ""
        explanations = {
            "hidden": (
                f"came back as '{HIDDEN_QUERY_TEXT}' — pg_stat_statements hides the text of other roles' "
                f"queries unless the role reading it is a superuser or has been granted pg_read_all_stats"
            ),
            "unparseable": (
                "could not be parsed — the statement may use grammar this parser does not know, or the "
                "recorded text may be incomplete; run with -vv to see them"
            ),
            "unplannable": (
                f"could not be planned{first}; for a query read from pg_stat_statements, which records it without its "
                "constants, the usual cause is a value of the wrong type substituted back in; run with -vv to see them"
            ),
        }
        reasons = [f"{count} {explanations[reason]}" for reason, count in dropped.items() if count]
        if not reasons:
            return None

        total = sum(dropped.values())
        skipped = f"{total} was skipped" if total == 1 else f"{total} were skipped"
        if kept:
            return f"Analyzing {kept} quer{'y' if kept == 1 else 'ies'}; {skipped}: " + "; ".join(reasons) + "."
        return f"None of the {total} queries in the workload could be analyzed: " + "; ".join(reasons) + "."

    def _covert_workload_to_query_weights(self, workload: list[dict[str, Any]]) -> list[tuple[str, SelectStmt, float]]:
        """Convert workload to query weights based on query frequency."""
        return [(q["query"], q["stmt"], self.convert_query_info_to_weight(q)) for q in workload]

    def convert_query_info_to_weight(self, query_info: dict[str, Any]) -> float:
        """Convert query info to weight based on query frequency."""
        return query_info.get("calls", 1.0) * query_info.get("avg_exec_time", 1.0)

    async def get_explain_plan_with_indexes(self, query_text: str, indexes: frozenset[IndexDefinition]) -> dict[str, Any]:
        """
        Get the explain plan for a query with a specific set of indexes.
        Results are memoized to avoid redundant explain operations.

        Args:
            query_text: The SQL query to explain
            indexes: A frozenset of IndexConfig objects representing the indexes to enable

        Returns:
            The explain plan as a dictionary
        """
        # Create a cache key from the query and indexes
        cache_key = (query_text, indexes)

        # Return cached result if available
        existing_plan = self._explain_plans_cache.get(cache_key)
        if existing_plan:
            return existing_plan

        # Generate the plan using the static method
        explain_plan_tool = ExplainPlanTool(self.sql_driver)
        plan = await explain_plan_tool.generate_explain_plan_with_hypothetical_indexes(query_text, indexes, False, self)

        # Cache the result
        self._explain_plans_cache[cache_key] = plan
        return plan

    def _get_workload_from_file(self, file_path: str) -> list[dict[str, Any]]:
        """Load queries from an SQL file."""
        try:
            with open(file_path, encoding="utf-8-sig") as f:
                content = f.read()

            # Split the file content by semicolons to get individual queries
            query_texts = [q.strip() for q in content.split(";") if q.strip()]
            queries = []

            for i, text in enumerate(query_texts):
                queries.append(
                    {
                        "queryid": i,
                        "query": text,
                    }
                )

            return queries
        except Exception as e:
            raise InvalidArgumentError(f"Error loading queries from file {file_path}: {e}") from e

    async def _get_query_stats(self, min_calls: int, min_avg_time_ms: float, limit: int) -> list[dict[str, Any]]:
        """Get query statistics from pg_stat_statements"""

        # Reference to original implementation
        return await self._get_query_stats_direct(min_calls, min_avg_time_ms, limit)

    async def _get_query_stats_direct(self, min_calls: int = 50, min_avg_time_ms: float = 5.0, limit: int = 100) -> list[dict[str, Any]]:
        """Direct implementation of query stats collection: this database's statements only."""
        stats = await locate_pg_stat_statements(self.sql_driver)
        if stats is None:
            # Without it there is no workload to read, which an empty result
            # would report as nothing to recommend.
            raise ExtensionRequiredError(
                PG_STAT_STATEMENTS,
                "The workload is read from pg_stat_statements, which is not installed in this database. "
                "Install it with CREATE EXTENSION pg_stat_statements and let it record the workload, "
                "or name the queries to tune instead.",
            )
        query = cast(
            LiteralString,
            f"""
        SELECT queryid, query, calls, {stats.total_time}/calls as avg_exec_time
        FROM {stats.relation}
        WHERE {THIS_DATABASE}
        AND calls >= {{}}
        AND {stats.total_time}/calls >= {{}}
        ORDER BY {stats.total_time} DESC
        LIMIT {{}}
        """,
        )
        result = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            query,
            [min_calls, min_avg_time_ms, limit],
        )
        return [dict(row.cells) for row in result] if result else []

    async def write_load(self, table: str) -> dict[str, float]:
        """How many writes on this table every index has to be maintained for.

        An insert adds an entry to every index; a delete marks one dead in
        every index. An update usually does both — but not always: a *HOT*
        update, one that changes no indexed column and finds room on the same
        page, updates no index at all. Counting plain updates would therefore
        overstate the cost on exactly the tables PostgreSQL is already
        optimising, so the HOT ones are subtracted.

        Returns:
            ``index_writes`` — the total an index would be maintained for —
            alongside the counters it came from, and ``hot_ratio``, the share
            of updates that currently avoid the indexes altogether.
        """
        if table in self._write_load_cache:
            return self._write_load_cache[table]

        schema, name = split_table_reference(table)
        query = """
            SELECT
                n_tup_ins AS inserts,
                n_tup_upd AS updates,
                n_tup_del AS deletes,
                n_tup_hot_upd AS hot_updates
            FROM pg_stat_user_tables
            WHERE relname = {}
        """
        params: list[Any] = [name]
        if schema:
            query += " AND schemaname = {}"
            params.append(schema)

        load: dict[str, float] = {"inserts": 0, "updates": 0, "deletes": 0, "hot_updates": 0, "index_writes": 0, "hot_ratio": 0.0}
        try:
            rows = await SafeSqlDriver.execute_param_query(self.sql_driver, query, params)
            if rows:
                cells = rows[0].cells
                inserts = int(cells["inserts"] or 0)
                updates = int(cells["updates"] or 0)
                deletes = int(cells["deletes"] or 0)
                hot = int(cells["hot_updates"] or 0)
                load = {
                    "inserts": inserts,
                    "updates": updates,
                    "deletes": deletes,
                    "hot_updates": hot,
                    "index_writes": inserts + deletes + max(updates - hot, 0),
                    "hot_ratio": round(hot / updates, 3) if updates else 0.0,
                }
        except Exception as e:
            # A workload is still worth tuning against a server that will not
            # say how much it is written to; the search just loses that term.
            logger.debug("Could not read the write load of %s: %s", table, e)

        self._write_load_cache[table] = load
        return load

    async def indexable_tables(self, tables: Iterable[str]) -> set[str]:
        """Keep the names that refer to something an index can be created on.

        Workloads are full of views, and asking HypoPG to index one raises an
        error that aborts the entire analysis — so they are dropped here rather
        than discovered at creation time. Names are resolved through
        ``to_regclass``, which applies the connection's search_path exactly as
        the query itself would. Anything in a system schema is dropped too: an
        index on a catalog table is not a recommendation anybody wants.
        """
        names = sorted({table for table in tables if table})
        if not names:
            return set()

        rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            """
            SELECT t.name AS name
            FROM unnest({}::text[]) AS t(name)
            JOIN pg_class c ON c.oid = to_regclass(t.name)
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = ANY({})
              AND n.nspname <> 'information_schema'
              AND n.nspname NOT LIKE 'pg\\_%'
            """,
            [names, list(INDEXABLE_RELKINDS)],
        )
        return {row.cells["name"] for row in rows} if rows else set()

    def _is_analyzable_stmt(self, stmt: Any) -> bool:
        """Check if a statement can be analyzed for index recommendations."""
        # It should be a SelectStmt
        if not isinstance(stmt, SelectStmt):
            return False

        visitor = TableAliasVisitor()
        visitor(stmt)

        # Skip queries that only access system tables
        if all(table.startswith("pg_") or table.startswith("aurora_") for table in visitor.tables):
            return False
        return True

    def dta_trace(self, message: Any, exc_info: bool = False):
        """Convenience function to log DTA thinking process."""

        # Always log to debug
        if exc_info:
            logger.debug(message, exc_info=True)
        else:
            logger.debug(message)

        self._dta_traces.append(message)

    async def _evaluate_configuration_cost(
        self,
        weighted_workload: list[tuple[str, SelectStmt, float]],
        indexes: frozenset[IndexDefinition],
    ) -> float:
        """Evaluate total cost with selective enabling and caching."""
        # Use indexes as cache key
        if indexes in self.cost_cache:
            self.dta_trace(f"  - Using cached cost for configuration: {candidate_str(indexes)}")
            return self.cost_cache[indexes]

        self.dta_trace(f"  - Evaluating cost for configuration: {candidate_str(indexes)}")

        total_cost = 0.0
        valid_queries = 0

        try:
            # Calculate cost for all queries with this configuration
            for query_text, _stmt, weight in weighted_workload:
                try:
                    # Get the explain plan using our memoized helper
                    plan_data = await self.get_explain_plan_with_indexes(query_text, indexes)

                    # Extract cost from the plan data
                    cost = self.extract_cost_from_json_plan(plan_data)
                    total_cost += cost * weight
                    valid_queries += 1
                except Exception as e:
                    raise IndexTuningError(f"Error executing explain for query: {query_text}: {e}") from e

            if valid_queries == 0:
                self.dta_trace("    + no valid queries found for cost evaluation")
                return float("inf")

            avg_cost = total_cost / valid_queries
            self.cost_cache[indexes] = avg_cost
            self.dta_trace(f"    + config cost: {avg_cost:.2f} (from {valid_queries} queries)")
            return avg_cost

        except Exception as e:
            self.dta_trace(f"    + error evaluating configuration: {e}")
            raise IndexTuningError(f"Error evaluating configuration: {e}") from e

    @staticmethod
    def _size_cache_key(table: str, columns: Iterable[str]) -> tuple[str, frozenset[str]]:
        return (table, frozenset(columns))

    def record_measured_index_size(self, table: str, columns: Iterable[str], size_bytes: int) -> None:
        """Remember a size HypoPG measured for an index on these columns.

        The planner's own estimate beats anything derived from pg_stats, so it
        is what the objective function, the size budget and the reported size
        should all use.
        """
        self._measured_size_cache[self._size_cache_key(table, columns)] = int(size_bytes)

    async def _estimate_index_size(self, table: str, columns: list[str]) -> int:
        cache_key = self._size_cache_key(table, columns)

        # A real measurement always wins over the formula below.
        measured = self._measured_size_cache.get(cache_key)
        if measured is not None:
            return measured

        # Check if we already have a cached result
        if cache_key in self._size_estimate_cache:
            return self._size_estimate_cache[cache_key]

        try:
            # Use parameterized query instead of f-string for security
            schema, table_name = split_table_reference(table)
            stats_query = """
            SELECT COALESCE(SUM(s.avg_width), 0) AS total_width,
                   COALESCE(SUM(s.n_distinct), 0) AS total_distinct,
                   MAX(c.reltuples) AS reltuples
            FROM pg_stats s
            JOIN pg_class c ON c.relname = s.tablename
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = s.schemaname
            WHERE s.tablename = {} AND s.attname = ANY({})
            """
            params: list[Any] = [table_name, columns]
            if schema:
                stats_query += " AND s.schemaname = {}"
                params.append(schema)

            result = await SafeSqlDriver.execute_param_query(
                self.sql_driver,
                stats_query,
                params,
            )
            if result and result[0].cells:
                size_estimate = self._estimate_index_size_internal(dict(result[0].cells))

                # Cache the result
                self._size_estimate_cache[cache_key] = size_estimate
                return size_estimate
            return 0
        except Exception as e:
            raise IndexTuningError(f"Error estimating index size: {e}") from e

    def _estimate_index_size_internal(self, stats: dict[str, Any]) -> int:
        """Rough size for an index HypoPG never measured.

        A btree holds one entry per live row, so the row count sets the size —
        not the column's distinctness. ``n_distinct`` is only a fallback for
        when the row count is unknown, and it needs care: pg_stats reports it
        as a *negative fraction* of the row count whenever the planner tracks
        distinctness that way (-1 means unique). Treating that as "one distinct
        value" priced every high-cardinality column, exactly the columns worth
        indexing, at a couple of dozen bytes.
        """
        width = (stats["total_width"] or 0) + 8  # 8 bytes for the heap TID

        # reltuples is -1 on a relation that has never been analyzed or
        # vacuumed (PostgreSQL 14+), and 0 on one that is genuinely empty.
        reltuples = float(stats.get("reltuples") or 0.0)
        entries = reltuples if reltuples > 0 else 0.0

        if entries <= 0:
            ndistinct = float(stats["total_distinct"] or 0.0)
            # Without a row count, a negative fraction cannot be resolved into
            # a count, so take its magnitude and accept the underestimate.
            entries = abs(ndistinct)

        # The factor stands in for per-entry overhead and the default fillfactor.
        return int(width * max(entries, 1.0) * 2.0)

    async def _format_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]], best_config: tuple[set[IndexRecommendation], float]
    ) -> list[IndexRecommendationAnalysis]:
        """Format recommendations into a list of IndexRecommendation objects."""
        # build final recommendations from best_config
        recommendations: list[IndexRecommendationAnalysis] = []
        total_size = 0
        budget_bytes = self.budget_mb * 1024 * 1024
        individual_base_cost = await self._evaluate_configuration_cost(query_weights, frozenset()) or 1.0
        progressive_base_cost = individual_base_cost
        indexes_so_far: list[IndexRecommendation] = []
        for index_config in best_config[0]:
            indexes_so_far.append(index_config)
            # Calculate the cost with only this index
            progressive_cost = await self._evaluate_configuration_cost(
                query_weights,
                frozenset(idx.index_definition for idx in indexes_so_far),  # Indexes so far
            )
            individual_cost = await self._evaluate_configuration_cost(
                query_weights,
                frozenset([index_config.index_definition]),  # Only this index
            )

            size = await self._estimate_index_size(index_config.table, list(index_config.columns))
            if budget_bytes < 0 or total_size + size <= budget_bytes:
                self.dta_trace(f"Adding index: {candidate_str([index_config])}")
                rec = IndexRecommendationAnalysis(
                    index_recommendation=IndexRecommendation(
                        table=index_config.table,
                        columns=index_config.columns,
                        using=index_config.using,
                        potential_problematic_reason=index_config.potential_problematic_reason,
                        estimated_size_bytes=size,
                    ),
                    progressive_base_cost=progressive_base_cost,
                    progressive_recommendation_cost=progressive_cost,
                    individual_base_cost=individual_base_cost,
                    individual_recommendation_cost=individual_cost,
                    queries=[q for q, _, _ in query_weights],
                    definition=index_config.definition,
                    write_load=await self.write_load(index_config.table),
                )
                progressive_base_cost = progressive_cost
                recommendations.append(rec)
                total_size += size
            else:
                self.dta_trace(f"Skipping index: {candidate_str([index_config])} because it exceeds budget")

        return recommendations

    @staticmethod
    def extract_cost_from_json_plan(plan_data: dict[str, Any]) -> float:
        """Extract total cost from JSON EXPLAIN plan data."""
        try:
            if not plan_data:
                return float("inf")

            # Parse JSON plan
            top_plan = plan_data.get("Plan")
            if not top_plan:
                logger.error("No top plan found in plan data: %s", plan_data)
                return float("inf")

            # Extract total cost from top plan
            total_cost = top_plan.get("Total Cost")
            if total_cost is None:
                logger.error("Total Cost not found in top plan: %s", top_plan)
                return float("inf")

            return float(total_cost)
        except (IndexError, KeyError, ValueError, json.JSONDecodeError) as e:
            raise IndexTuningError(f"Error extracting cost from plan: {e}") from e

    async def _get_table_size(self, table: str) -> int:
        """
        Get the total size of a table including indexes and toast tables.
        Uses memoization to avoid repeated database queries.

        Args:
            table: The name of the table

        Returns:
            Size of the table in bytes
        """
        # Check if we have a cached result
        if table in self._table_size_cache:
            return self._table_size_cache[table]

        # Try to get table size from the database using proper quoting
        try:
            # to_regclass resolves a possibly schema-qualified name and returns
            # NULL rather than raising when the table does not exist.
            query = "SELECT pg_total_relation_size(to_regclass({})) as rel_size"
            result = await SafeSqlDriver.execute_param_query(self.sql_driver, query, [table_identifier(table).as_string(None)])

            if result and len(result) > 0 and result[0].cells.get("rel_size") is not None:
                size = int(result[0].cells["rel_size"])
                # Cache the result
                self._table_size_cache[table] = size
                return size
            else:
                # If query fails, use our estimation method
                size = await self._estimate_table_size(table)
                self._table_size_cache[table] = size
                return size
        except Exception as e:
            logger.warning("Error getting table size for %s: %s", table, e)
            # Use estimation method
            size = await self._estimate_table_size(table)
            self._table_size_cache[table] = size
            return size

    async def _estimate_table_size(self, table: str) -> int:
        """Estimate the size of a table if we can't get it from the database."""
        try:
            # Try a simple query to get row count and then estimate size. The
            # table name is an identifier, not a literal, so it is passed as a
            # composed identifier rather than a bound parameter.
            result = await SafeSqlDriver.execute_param_query(self.sql_driver, "SELECT count(*) as row_count FROM {}", [table_identifier(table)])
            if result and len(result) > 0 and len(result[0].cells) > 0:
                row_count = int(result[0].cells["row_count"])
                # Rough estimate: assume 1KB per row
                return row_count * 1024
        except Exception as e:
            logger.warning("Error estimating table size for %s: %s", table, e)

        # Default size if we can't estimate
        return 10 * 1024 * 1024  # 10MB default

    @abstractmethod
    async def _generate_recommendations(self, query_weights: list[tuple[str, SelectStmt, float]]) -> tuple[set[IndexRecommendation], float]:
        """Generate index tuning queries."""
        pass
