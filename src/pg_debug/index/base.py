import json
import logging
import re
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterable
from collections.abc import Iterator
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
from ..db.statements import StatementStatistics
from ..db.statements import locate_pg_stat_statements
from ..db.window import statement_window
from ..errors import ExtensionRequiredError
from ..errors import IndexTuningError
from ..errors import InvalidArgumentError
from ..errors import PgDebugError
from ..parsing import SqlBindParams
from ..parsing import TableAliasVisitor
from ..plans import ExplainPlanTool
from ..plans.hypothetical import IndexDefinition
from ..plans.tree import calculate_improvement_multiple
from ..queries.hypopg import HYPOPG_RESET_SQL

logger = logging.getLogger(__name__)

#: Whether any table here has planner statistics.
ANALYZED_SQL = """
SELECT
    EXISTS (SELECT 1 FROM pg_stats WHERE schemaname NOT IN ('pg_catalog', 'information_schema'))
    OR EXISTS (SELECT 1 FROM pg_stat_user_tables WHERE last_analyze IS NOT NULL OR last_autoanalyze IS NOT NULL)
    AS analyzed
"""

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

#: How far the recommended set may raise any one query's planner cost, as a
#: fraction of what the query cost before: 0.1 lets a query cost up to 1.1
#: times its baseline. It is Azure's default for the same bound
#: (``index_tuning.max_regression_factor``), and like Azure's it bounds the
#: planner's cost, not the time a query takes.
#:
#: The bound is a judgment, not a measurement. The planner prunes paths whose
#: costs lie within about 1% of each other, and a join search over many
#: tables is not exhaustive, so a few percent either way is noise; a tenth is
#: well above that and well below a plan that has really gone wrong. Set it
#: to 0 to refuse any rise at all.
DEFAULT_MAX_REGRESSION_FACTOR = 0.1

#: Wider than this, a merged index is not proposed: the advisor's own
#: candidates stop at three columns, and a merge should not build what the
#: search itself would not.
DEFAULT_MAX_INDEX_WIDTH = 3

# Sizes of the hypothetical indexes created so far.
#
# This reads the hypopg() function rather than the hypopg_list_indexes view it
# backs. HypoPG's extension script grants no privileges on its views, and a
# view is private to its owner by default, so any role that did not create the
# extension gets "permission denied for view hypopg_list_indexes". Functions
# default to EXECUTE for PUBLIC, and hypopg() exposes both columns needed here.
HYPOTHETICAL_INDEX_SIZES_SQL = "SELECT indexname AS index_name, hypopg_relation_size(indexrelid) AS index_size FROM hypopg();"

#: HypoPG names a hypothetical index ``<oid>`` followed by its access method,
#: its table and its columns, truncated to an identifier's 63 bytes, whatever
#: name the CREATE INDEX statement gave it. Plans show that name.
_HYPOTHETICAL_NAME = re.compile(r"^<\d+>(.*)$")
_MAX_IDENTIFIER_BYTES = 63

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
    #: The workload queries whose plan, with the whole recommended set in
    #: place, reads this index.
    queries_using: list[str] = field(default_factory=list)

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

    #: The regression bound: the factor, how many queries it checked, the
    #: worst ratio left, and each index it refused with the queries it
    #: raised. None when nothing was recommended to check.
    regression_check: dict[str, Any] | None = None
    #: Pairs of recommended indexes sharing a leading column: merged into
    #: one where the merged index served every query within the bound, and
    #: refused with the figure where it did not.
    consolidations: list[dict[str, Any]] = field(default_factory=list)
    #: Candidates evaluated and not recommended, with their costs and why:
    #: the ``llm`` method's alternatives, and any index no query's plan reads
    #: once the rest of the set is in place.
    refused_candidates: list[dict[str, Any]] = field(default_factory=list)
    #: For a workload read from pg_stat_statements, how long it has been
    #: counting: the window the sample was drawn from. None otherwise, or
    #: when it could not be read.
    workload_window: dict[str, Any] | None = None


def candidate_str(indexes: Iterable[IndexDefinition] | Iterable[IndexRecommendation] | Iterable[IndexRecommendationAnalysis]) -> str:
    return ", ".join(f"{idx.table}({','.join(idx.columns)})" for idx in indexes) if indexes else "(no indexes)"


class IndexTuningBase(ABC):
    def __init__(
        self,
        sql_driver: SqlDriver,
        max_regression_factor: float = DEFAULT_MAX_REGRESSION_FACTOR,
    ):
        """
        :param sql_driver: Database access
        :param max_regression_factor: How far the recommended set may raise
            any one query's planner cost above its cost without it, as a
            fraction (0.1 is 10%). See DEFAULT_MAX_REGRESSION_FACTOR.

        Raises:
            InvalidArgumentError: If the factor is negative.
        """
        if max_regression_factor < 0:
            raise InvalidArgumentError(f"max_regression_factor must be 0 or more, not {max_regression_factor}.")
        self.sql_driver = sql_driver
        self.max_regression_factor = max_regression_factor
        self.max_index_width = DEFAULT_MAX_INDEX_WIDTH

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

        # Where the workload was read from, when it came from
        # pg_stat_statements, so its counting window can be reported.
        self._statement_stats: StatementStatistics | None = None
        # What _format_recommendations checked and refused on the way.
        self._regression_check: dict[str, Any] | None = None
        self._consolidations: list[dict[str, Any]] = []
        # What the method evaluated and did not choose; see LLMOptimizerTool.
        self._refused_candidates: list[dict[str, Any]] = []

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
        self._statement_stats = None
        self._regression_check = None
        self._consolidations = []
        self._refused_candidates = []

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
                    session.regression_check = self._regression_check
                    session.consolidations = self._consolidations
                    session.refused_candidates = self._refused_candidates

                if session.workload_source == "query_store":
                    session.workload_window = await self._workload_window()

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

        # Pre-check 2: the planner has statistics to plan with. Read from
        # pg_stats as well as the times of the last ANALYZE: a crash or
        # pg_stat_reset() clears the times but not the statistics, and
        # autovacuum's ANALYZE sets only last_autoanalyze.
        result = await self.sql_driver.execute_query(ANALYZED_SQL)
        if not result or not result[0].cells.get("analyzed"):
            raise IndexTuningError(
                "No table in this database has planner statistics, so every plan the advisor compared would rest on "
                "guesses. Run ANALYZE on the tables the workload reads, then tune again."
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
        self._statement_stats = stats
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

    async def _workload_window(self) -> dict[str, Any] | None:
        """How long pg_stat_statements had been counting when the workload was read from it.

        HypoPG's costs are deterministic: the same queries, statistics and
        index set plan to the same cost on every run. The workload is not. It
        is the costliest statements recorded since the last reset, so another
        run, after more traffic, a reset or an eviction, can tune for other
        queries; the window says what this run's sample covered.
        """
        if self._statement_stats is None:
            return None
        try:
            return (await statement_window(self.sql_driver, self._statement_stats)).to_dict()
        except Exception as e:
            # The recommendations stand without it; the reader is told the
            # window is unknown rather than given none.
            logger.debug("Could not read pg_stat_statements' counting window: %s", e)
            return None

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

    async def measure_index_sizes(self, indexes: list[IndexRecommendation]) -> None:
        """Have HypoPG measure each index, and keep its numbers on the index and in the cache.

        The creates and the measurement go in one transaction on purpose.
        Hypothetical indexes live in the backend that created them, and
        neither the connection pool nor a transaction pooler in front of the
        server promises the next statement the same backend — measuring in a
        second call can land on another and see no indexes at all, which
        would silently make every candidate look free. Only the last
        statement's results come back, so the size query goes last, and the
        reset runs after it in the same transaction.
        """
        if not indexes:
            return
        create_and_measure = (
            SafeSqlDriver.param_sql_to_query(
                "SELECT hypopg_create_index({});" * len(indexes),
                [idx.definition for idx in indexes],
            )
            + HYPOTHETICAL_INDEX_SIZES_SQL
        )
        result = await self.sql_driver.execute_query(cast(LiteralString, create_and_measure), cleanup=HYPOPG_RESET_SQL)
        if result is None:
            return
        index_map = {r.cells["index_name"]: r.cells["index_size"] for r in result}
        for idx in indexes:
            if idx.name in index_map:
                idx.estimated_size_bytes = index_map[idx.name]
                # The planner's own number, so the objective function, the
                # size budget and the reported size all use it instead of the
                # pg_stats guess.
                self.record_measured_index_size(idx.table, idx.columns, idx.estimated_size_bytes)

    async def _query_costs(self, query_weights: list[tuple[str, SelectStmt, float]], indexes: Iterable[IndexRecommendation]) -> list[float]:
        """Each query's own planner cost with these indexes, unweighted, in workload order."""
        configuration = frozenset(idx.index_definition for idx in indexes)
        return [self.extract_cost_from_json_plan(await self.get_explain_plan_with_indexes(query, configuration)) for query, _, _ in query_weights]

    async def _index_size(self, index: IndexRecommendation) -> int:
        """The index's size: HypoPG's measurement where there is one, measured now where not."""
        key = self._size_cache_key(index.table, index.columns)
        if key not in self._measured_size_cache:
            try:
                await self.measure_index_sizes([index])
            except Exception as e:
                logger.debug("Could not measure %s: %s", index.definition, e)
        return await self._estimate_index_size(index.table, list(index.columns))

    async def _format_recommendations(
        self, query_weights: list[tuple[str, SelectStmt, float]], best_config: tuple[set[IndexRecommendation], float]
    ) -> list[IndexRecommendationAnalysis]:
        """Check the chosen set, then describe each index in it.

        The search optimises the workload's total, which an improvement to one
        query can carry past a regression in another. So before anything is
        recommended, indexes that share a leading column are merged where one
        index serves both (:meth:`_consolidate`), the size budget is applied,
        and every query is costed again against the final set, refusing an
        index that raises any query past the regression bound
        (:meth:`_bound_regressions`).
        """
        budget_mb = getattr(self, "budget_mb", -1)
        budget_bytes = budget_mb * 1024 * 1024 if budget_mb > 0 else -1
        individual_base_cost = await self._evaluate_configuration_cost(query_weights, frozenset()) or 1.0

        # Most beneficial on its own first: the order to create them in, and
        # the same order on every run, which a set's iteration order is not.
        individual: dict[IndexRecommendation, float] = {}
        for index in best_config[0]:
            individual[index] = await self._evaluate_configuration_cost(query_weights, frozenset([index.index_definition]))
        chosen = sorted(best_config[0], key=lambda idx: (individual[idx], idx.table, idx.columns))

        chosen = await self._consolidate(query_weights, chosen)

        within_budget: list[IndexRecommendation] = []
        total_size = 0
        for index in chosen:
            size = await self._index_size(index)
            if budget_bytes < 0 or total_size + size <= budget_bytes:
                within_budget.append(index)
                total_size += size
            else:
                self.dta_trace(f"Skipping index: {candidate_str([index])} because it exceeds budget")

        final = await self._bound_regressions(query_weights, within_budget) if within_budget else []

        queries_using = await self._queries_using(query_weights, final)
        final = await self._drop_unread(query_weights, final, queries_using, individual_base_cost)

        recommendations: list[IndexRecommendationAnalysis] = []
        progressive_base_cost = individual_base_cost
        indexes_so_far: list[IndexRecommendation] = []
        for index in final:
            indexes_so_far.append(index)
            progressive_cost = await self._evaluate_configuration_cost(query_weights, frozenset(idx.index_definition for idx in indexes_so_far))
            individual_cost = await self._evaluate_configuration_cost(query_weights, frozenset([index.index_definition]))
            self.dta_trace(f"Adding index: {candidate_str([index])}")
            recommendations.append(
                IndexRecommendationAnalysis(
                    index_recommendation=IndexRecommendation(
                        table=index.table,
                        columns=index.columns,
                        using=index.using,
                        potential_problematic_reason=index.potential_problematic_reason,
                        estimated_size_bytes=await self._index_size(index),
                    ),
                    progressive_base_cost=progressive_base_cost,
                    progressive_recommendation_cost=progressive_cost,
                    individual_base_cost=individual_base_cost,
                    individual_recommendation_cost=individual_cost,
                    queries=[q for q, _, _ in query_weights],
                    definition=index.definition,
                    write_load=await self.write_load(index.table),
                    queries_using=queries_using.get(index, []),
                )
            )
            progressive_base_cost = progressive_cost

        return recommendations

    async def _bound_regressions(
        self, query_weights: list[tuple[str, SelectStmt, float]], indexes: list[IndexRecommendation]
    ) -> list[IndexRecommendation]:
        """Refuse indexes until no query costs more than the bound allows over its baseline.

        Every query is planned again with the whole set in place, and its
        cost compared with its cost without any of it. While one exceeds
        ``1 + max_regression_factor`` times its baseline, the index whose
        removal leaves the fewest queries over the bound (and then the lowest
        workload cost) is refused, with the queries it was in the set for.
        Removing them all restores every baseline, so this ends.

        Records what it checked in ``self._regression_check``.
        """
        limit = 1.0 + self.max_regression_factor
        baseline = await self._query_costs(query_weights, [])
        kept = list(indexes)
        refused: list[dict[str, Any]] = []

        while kept:
            costs = await self._query_costs(query_weights, kept)
            regressed = _over_bound(baseline, costs, limit)
            if not regressed:
                break
            best: tuple[tuple[int, float], IndexRecommendation] | None = None
            for index in kept:
                rest = [idx for idx in kept if idx != index]
                rest_costs = await self._query_costs(query_weights, rest)
                rest_total = await self._evaluate_configuration_cost(query_weights, frozenset(i.index_definition for i in rest))
                score = (len(_over_bound(baseline, rest_costs, limit)), rest_total)
                if best is None or score < best[0]:
                    best = (score, index)
            assert best is not None
            dropped = best[1]
            kept.remove(dropped)
            self.dta_trace(
                f"Refusing index: {candidate_str([dropped])}: with it, {len(regressed)} queries cost more than {limit:.2f} times their baseline"
            )
            refused.append(
                {
                    "index": dropped.definition,
                    "table": dropped.table,
                    "columns": list(dropped.columns),
                    "queries": [_cost_evidence(query_weights[i][0], baseline[i], costs[i]) for i in regressed],
                }
            )

        final_costs = await self._query_costs(query_weights, kept)
        ratio, at = _worst_ratio(baseline, final_costs)
        self._regression_check = {
            "max_regression_factor": self.max_regression_factor,
            "queries_checked": len(query_weights),
            "worst_query": _cost_evidence(query_weights[at][0], baseline[at], final_costs[at]) if at is not None else None,
            "refused": refused,
        }
        return kept

    async def _consolidate(self, query_weights: list[tuple[str, SelectStmt, float]], indexes: list[IndexRecommendation]) -> list[IndexRecommendation]:
        """Merge two recommended indexes that share a leading column where one index serves both.

        Index merging, after Chaudhuri and Narasayya (1999): for two btree
        indexes on one table whose first column is the same, the merged index
        is the wider one when one's columns lead the other's, and otherwise
        one's columns followed by the other's remaining ones. It replaces both
        if no query then costs more than ``1 + max_regression_factor`` times
        what it cost with the two; the merged index is one index to store and
        maintain instead of two. A pair that fails is kept, and the refusal
        recorded with the query and the figure it failed on.

        Records each merge and each refusal in ``self._consolidations``.
        """
        limit = 1.0 + self.max_regression_factor
        kept = list(indexes)
        tried: set[frozenset[IndexDefinition]] = set()
        merged_one = True
        while merged_one:
            merged_one = False
            reference: list[float] | None = None
            for first, second in _pairs_sharing_a_leading_column(kept):
                pair = frozenset({first.index_definition, second.index_definition})
                if pair in tried:
                    continue
                tried.add(pair)
                if reference is None:
                    reference = await self._query_costs(query_weights, kept)

                passed: tuple[float, list[IndexRecommendation], IndexRecommendation, float] | None = None
                failed: dict[str, Any] = {}
                for columns in _merged_columns(first.columns, second.columns):
                    if len(columns) > self.max_index_width:
                        failed.setdefault(
                            "reason", f"the merged index would have {len(columns)} columns, more than the {self.max_index_width} allowed"
                        )
                        continue
                    merged = (
                        first
                        if columns == first.columns
                        else second
                        if columns == second.columns
                        else IndexRecommendation(first.table, columns, first.using)
                    )
                    trial = [idx for idx in kept if idx not in (first, second)] + [merged]
                    costs = await self._query_costs(query_weights, trial)
                    ratio, worst = _worst_ratio(reference, costs)
                    if ratio <= limit:
                        total = await self._evaluate_configuration_cost(query_weights, frozenset(i.index_definition for i in trial))
                        if passed is None or total < passed[0]:
                            passed = (total, trial, merged, ratio)
                    elif worst is not None and ("ratio" not in failed or ratio < failed["ratio"]):
                        failed.update(
                            {
                                "tried": merged.definition,
                                "ratio": round(ratio, 3),
                                "query": query_weights[worst][0],
                                "cost_with_both": round(reference[worst], 2),
                                "cost_with_merged": round(costs[worst], 2),
                                "reason": (
                                    f"with {merged.definition} in place of both, a query costs {ratio:.2f} times what it costs "
                                    f"with the two, over the bound of {limit:.2f}"
                                ),
                            }
                        )

                both = [first.definition, second.definition]
                if passed is None:
                    self.dta_trace(f"Not merging {candidate_str([first, second])}: {failed.get('reason')}")
                    self._consolidations.append({"merged": False, "indexes": both, **failed})
                    continue

                _, trial, merged, ratio = passed
                size_before = await self._index_size(first) + await self._index_size(second)
                size_after = await self._index_size(merged)
                self.dta_trace(f"Merging {candidate_str([first, second])} into {candidate_str([merged])}")
                self._consolidations.append(
                    {
                        "merged": True,
                        "indexes": both,
                        "into": merged.definition,
                        "worst_ratio": round(ratio, 3),
                        "size_before_bytes": size_before,
                        "size_after_bytes": size_after,
                    }
                )
                kept = trial
                merged_one = True
                break
        return kept

    async def _drop_unread(
        self,
        query_weights: list[tuple[str, SelectStmt, float]],
        indexes: list[IndexRecommendation],
        queries_using: dict[IndexRecommendation, list[str]],
        base_cost: float,
    ) -> list[IndexRecommendation]:
        """Refuse the indexes no query's plan reads once the whole set is in place.

        The greedy search adds one index at a time, and an index chosen early
        can be displaced by one chosen later: each query's plan then reads the
        later one, and the earlier only costs writes. Whether a plan reads an
        index is matched by HypoPG's name for it, so before refusing one the
        planner is asked as well: without it, no query may cost more than with
        it. An index whose name did not match but whose removal costs
        something is kept.
        """
        unread = [index for index in indexes if not queries_using.get(index)]
        if not unread:
            return indexes
        costs = await self._query_costs(query_weights, indexes)
        kept = list(indexes)
        for index in unread:
            rest = [idx for idx in kept if idx != index]
            if any(without > with_it * (1 + 1e-9) for without, with_it in zip(await self._query_costs(query_weights, rest), costs, strict=True)):
                logger.debug("No plan named %s, but removing it raises a cost; keeping it", index.definition)
                continue
            kept = rest
            alone = await self._evaluate_configuration_cost(query_weights, frozenset([index.index_definition]))
            self.dta_trace(f"Refusing index: {candidate_str([index])}: no query's plan reads it with the rest of the set in place")
            self._refused_candidates.append(
                {
                    "indexes": [index.definition],
                    "execution_cost": round(alone, 2),
                    "baseline_cost": round(base_cost, 2),
                    "reason": (
                        "with the rest of the recommended set in place no query's plan reads it, and removing it raises no "
                        "query's cost: an index chosen later serves its queries, and it would only cost writes"
                    ),
                }
            )
        return kept

    async def _queries_using(
        self, query_weights: list[tuple[str, SelectStmt, float]], indexes: list[IndexRecommendation]
    ) -> dict[IndexRecommendation, list[str]]:
        """For each index, the queries whose plan reads it with the whole set in place."""
        using: dict[IndexRecommendation, list[str]] = {index: [] for index in indexes}
        if not indexes:
            return using
        configuration = frozenset(idx.index_definition for idx in indexes)
        for query, _, _ in query_weights:
            names = set(_index_names(await self.get_explain_plan_with_indexes(query, configuration)))
            for index in indexes:
                if any(is_hypothetical_name_of(name, index.index_definition) for name in names):
                    using[index].append(query)
        return using

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


def _cost_evidence(query: str, baseline: float, cost: float) -> dict[str, Any]:
    return {
        "query": query,
        "baseline_cost": round(baseline, 2),
        "cost_with_set": round(cost, 2),
        "ratio": round(cost / baseline, 3) if baseline > 0 else None,
    }


def _over_bound(baseline: list[float], costs: list[float], limit: float) -> list[int]:
    """The positions of the queries that cost more than ``limit`` times their baseline."""
    return [i for i, (base, cost) in enumerate(zip(baseline, costs, strict=True)) if base > 0 and cost > base * limit]


def _worst_ratio(reference: list[float], costs: list[float]) -> tuple[float, int | None]:
    """The largest ratio of cost to reference, and the query it belongs to."""
    worst, at = 0.0, None
    for i, (base, cost) in enumerate(zip(reference, costs, strict=True)):
        if base > 0 and (at is None or cost / base > worst):
            worst, at = cost / base, i
    return worst, at


def _pairs_sharing_a_leading_column(indexes: list[IndexRecommendation]) -> Iterator[tuple[IndexRecommendation, IndexRecommendation]]:
    for i, first in enumerate(indexes):
        for second in indexes[i + 1 :]:
            if first.table == second.table and first.using == second.using == "btree" and first.columns[:1] == second.columns[:1]:
                yield first, second


def _merged_columns(first: tuple[str, ...], second: tuple[str, ...]) -> list[tuple[str, ...]]:
    """The indexes that could replace both: the wider, when one leads the other; else each followed by the rest of the other."""
    if second[: len(first)] == first:
        return [second]
    if first[: len(second)] == second:
        return [first]
    options = [first + tuple(c for c in second if c not in first), second + tuple(c for c in first if c not in second)]
    return list(dict.fromkeys(options))


def _index_names(plan: dict[str, Any]) -> Iterator[str]:
    """Every index name a JSON plan reads, at any depth."""
    stack: list[Any] = [plan.get("Plan")]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        name = node.get("Index Name")
        if isinstance(name, str):
            yield name
        stack.extend(node.get("Plans") or [])


def is_hypothetical_name_of(name: str, index: IndexDefinition) -> bool:
    """Whether a plan's index name is HypoPG's name for this index.

    HypoPG ignores the name in the CREATE INDEX statement and calls the
    index ``<oid>``, its access method, its table and its columns, joined
    with underscores and cut at 63 bytes. A cut name is matched as a prefix,
    which only a name of the full 63 bytes can be.
    """
    match = _HYPOTHETICAL_NAME.match(name)
    if not match:
        return False
    stem = match.group(1)
    _, table = split_table_reference(index.table)
    expected = "_".join([index.using, table, *index.columns])
    if stem == expected:
        return True
    return len(name.encode()) >= _MAX_IDENTIFIER_BYTES and expected.startswith(stem)
