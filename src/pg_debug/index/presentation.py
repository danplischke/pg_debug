"""Database Tuning Advisor (DTA) tool for Postgres MCP."""

import logging
import os
from typing import Any

import humanize

from ..db import SqlDriver
from ..plans.tree import ExplainPlanArtifact
from ..plans.tree import calculate_improvement_multiple
from .base import IndexDefinition
from .base import IndexTuningResult
from .dta import IndexTuningBase

logger = logging.getLogger(__name__)

#: Above this share of HOT updates, an index over the wrong column does more
#: than add maintenance: it stops PostgreSQL avoiding the indexes altogether.
HOT_UPDATE_WARNING_RATIO = 0.5


class TextPresentation:
    """Text-based presentation of index tuning recommendations."""

    def __init__(self, sql_driver: SqlDriver, index_tuning: IndexTuningBase):
        """
        Initialize the presentation.

        Args:
            conn: The PostgreSQL connection object
        """
        self.sql_driver = sql_driver
        self.index_tuning = index_tuning

    async def analyze_workload(self, max_index_size_mb=10000):
        """
        Analyze SQL workload and recommend indexes.

        This method analyzes queries from database query history, examining
        frequently executed and costly queries to recommend the most beneficial indexes.

        Args:
            max_index_size_mb: Maximum total size for recommended indexes in MB

        Returns:
            Dict with recommendations or error
        """
        return await self._execute_analysis(
            min_calls=50,
            min_avg_time_ms=5.0,
            limit=100,
            max_index_size_mb=max_index_size_mb,
        )

    async def analyze_queries(self, queries, max_index_size_mb=10000):
        """
        Analyze a list of SQL queries and recommend indexes.

        This method examines the provided SQL queries and recommends
        indexes that would improve their performance.

        Args:
            queries: List of SQL queries to analyze
            max_index_size_mb: Maximum total size for recommended indexes in MB

        Returns:
            Dict with recommendations or error
        """
        if not queries:
            return {"error": "No queries provided for analysis"}

        return await self._execute_analysis(
            query_list=queries,
            min_calls=0,  # Ignore min calls for explicit query list
            min_avg_time_ms=0,  # Ignore min time for explicit query list
            limit=0,  # Ignore limit for explicit query list
            max_index_size_mb=max_index_size_mb,
        )

    async def analyze_single_query(self, query, max_index_size_mb=10000):
        """
        Analyze a single SQL query and recommend indexes.

        This method examines the provided SQL query and recommends
        indexes that would improve its performance.

        Args:
            query: SQL query to analyze
            max_index_size_mb: Maximum total size for recommended indexes in MB

        Returns:
            Dict with recommendations or error
        """
        return await self._execute_analysis(
            query_list=[query],
            min_calls=0,  # Ignore min calls for explicit query
            min_avg_time_ms=0,  # Ignore min time for explicit query
            limit=0,  # Ignore limit for explicit query
            max_index_size_mb=max_index_size_mb,
        )

    async def _execute_analysis(
        self,
        query_list=None,
        min_calls=50,
        min_avg_time_ms=5.0,
        limit=100,
        max_index_size_mb=10000,
    ):
        """
        Execute indexing analysis

        Returns:
            Dict with recommendations or dict with error
        """
        try:
            # Run the index tuning analysis
            session = await self.index_tuning.analyze_workload(
                query_list=query_list,
                min_calls=min_calls,
                min_avg_time_ms=min_avg_time_ms,
                limit=limit,
                max_index_size_mb=max_index_size_mb,
            )

            # Tuning traces are verbose and only useful when debugging the
            # advisor itself, so they are opt-in via PG_DEBUG_INCLUDE_TRACE=true.
            include_trace = os.environ.get("PG_DEBUG_INCLUDE_TRACE", "false").lower() == "true"
            langfuse_trace = {"_trace": session.dta_traces} if include_trace else {}

            if session.error:
                return {
                    "error": session.error,
                    **langfuse_trace,
                }

            if not session.recommendations:
                return {
                    "recommendations": "No index recommendations found.",
                    **langfuse_trace,
                }

            # Calculate overall statistics
            total_size_bytes = sum(rec.estimated_size_bytes for rec in session.recommendations)

            # Calculate overall performance improvement
            initial_cost = session.recommendations[0].progressive_base_cost if session.recommendations else 0
            new_cost = session.recommendations[-1].progressive_recommendation_cost if session.recommendations else 1.0
            improvement_multiple = calculate_improvement_multiple(initial_cost, new_cost)

            # Build recommendations list
            recommendations = self._build_recommendations_list(session)

            # Generate query impact section using helper function
            query_impact = await self._generate_query_impact(session)

            # Create the result JSON object with summary, recommendations, and query impact
            return {
                "summary": {
                    "total_recommendations": len(session.recommendations),
                    "base_cost": f"{initial_cost:.1f}",
                    "new_cost": f"{new_cost:.1f}",
                    "total_size_bytes": humanize.naturalsize(total_size_bytes),
                    "improvement_multiple": f"{improvement_multiple:.1f}",
                },
                "recommendations": recommendations,
                "query_impact": query_impact,
                **langfuse_trace,
            }
        except Exception as e:
            logger.error(f"Error analyzing queries: {e}", exc_info=True)
            return {"error": f"Error analyzing queries: {e}"}

    def _build_recommendations_list(self, session: IndexTuningResult) -> list[dict[str, Any]]:
        recommendations = []
        for index_apply_order, rec in enumerate(session.recommendations):
            rec_dict = {
                "index_apply_order": index_apply_order + 1,
                "index_target_table": rec.table,
                "index_target_columns": rec.columns,
                "benefit_of_this_index_only": {
                    "improvement_multiple": f"{rec.individual_improvement_multiple:.1f}",
                    "base_cost": f"{rec.individual_base_cost:.1f}",
                    "new_cost": f"{rec.individual_recommendation_cost:.1f}",
                },
                "benefit_after_previous_indexes": {
                    "improvement_multiple": f"{rec.progressive_improvement_multiple:.1f}",
                    "base_cost": f"{rec.progressive_base_cost:.1f}",
                    "new_cost": f"{rec.progressive_recommendation_cost:.1f}",
                },
                "index_estimated_size": humanize.naturalsize(rec.estimated_size_bytes),
                "index_definition": rec.definition,
            }
            write_cost = self._describe_write_cost(rec)
            if write_cost:
                rec_dict["write_cost"] = write_cost
            if rec.potential_problematic_reason == "long_text_column":
                rec_dict["warning"] = (
                    "This index includes a text column with no length limit. A btree entry has to fit "
                    "in roughly 2704 bytes — about a third of a page — so CREATE INDEX will fail on any "
                    "row whose values are longer than that. Consider indexing an expression over the "
                    "column instead, such as left(col, 100) or md5(col)."
                )
            elif rec.potential_problematic_reason:
                rec_dict["warning"] = f"This index is potentially problematic because it includes a {rec.potential_problematic_reason} column."
            recommendations.append(rec_dict)
        return recommendations

    @staticmethod
    def _describe_write_cost(rec) -> dict[str, Any] | None:
        """What this index will cost to keep up to date.

        An index is not paid for once. Every insert, every delete and every
        update that changes an indexed column has to maintain it, and those
        counts are already being kept — so the benefit and the cost can be
        reported side by side rather than only the benefit.
        """
        load = getattr(rec, "write_load", None) or {}
        index_writes = int(load.get("index_writes") or 0)
        if not index_writes:
            return None

        cost: dict[str, Any] = {
            "index_touching_writes": index_writes,
            "inserts": int(load.get("inserts") or 0),
            "updates": int(load.get("updates") or 0),
            "deletes": int(load.get("deletes") or 0),
            "note": (
                f"{index_writes:,} writes recorded on {rec.table} have to maintain every index on it, this one "
                f"included. Inserts and deletes always do; updates only when they change an indexed column."
            ),
        }

        hot_ratio = float(load.get("hot_ratio") or 0.0)
        if hot_ratio >= HOT_UPDATE_WARNING_RATIO:
            cost["hot_update_warning"] = (
                f"{hot_ratio:.0%} of updates to {rec.table} are currently HOT — they rewrite the row without "
                f"touching any index at all. An index over a column those updates change gives that up, and the "
                f"table starts bloating faster as well as writing more."
            )
        return cost

    async def _generate_query_impact(self, session: IndexTuningResult) -> list[dict[str, Any]]:
        """
        Generate the query impact section showing before/after explain plans.

        Args:
            session: DTASession containing recommendations

        Returns:
            List of dictionaries with query and explain plans
        """
        query_impact = []

        # Get workload queries from the first recommendation
        # (All recommendations have the same queries)
        if not session.recommendations:
            return query_impact

        workload_queries = session.recommendations[0].queries

        # Remove duplicates while preserving order
        seen = set()
        unique_queries = []
        for q in workload_queries:
            if q not in seen:
                seen.add(q)
                unique_queries.append(q)

        # Get before and after plans for each query
        if unique_queries and self.index_tuning:
            for query in unique_queries:
                # Get plan with no indexes
                before_plan = await self.index_tuning.get_explain_plan_with_indexes(query, frozenset())

                # Get plan with all recommended indexes
                index_configs = frozenset(IndexDefinition(rec.table, rec.columns, rec.using) for rec in session.recommendations)
                after_plan = await self.index_tuning.get_explain_plan_with_indexes(query, index_configs)

                # Extract costs from plans
                base_cost = self.index_tuning.extract_cost_from_json_plan(before_plan)
                new_cost = self.index_tuning.extract_cost_from_json_plan(after_plan)

                # Calculate improvement multiple
                improvement_multiple = "∞"  # Default for cases where new_cost is zero
                if new_cost > 0 and base_cost > 0:
                    improvement_multiple = f"{calculate_improvement_multiple(base_cost, new_cost):.1f}"

                before_plan_text = ExplainPlanArtifact.format_plan_summary(before_plan)
                after_plan_text = ExplainPlanArtifact.format_plan_summary(after_plan)
                diff_text = ExplainPlanArtifact.create_plan_diff(before_plan, after_plan)

                # Add to query impact with costs and improvement
                query_impact.append(
                    {
                        "query": query,
                        "base_cost": f"{base_cost:.1f}",
                        "new_cost": f"{new_cost:.1f}",
                        "improvement_multiple": improvement_multiple,
                        "before_explain_plan": "```\n" + before_plan_text + "\n```",
                        "after_explain_plan": "```\n" + after_plan_text + "\n```",
                        "explain_plan_diff": "```\n" + diff_text + "\n```",
                    }
                )

        return query_impact
