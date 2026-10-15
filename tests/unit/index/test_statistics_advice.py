"""Index candidates from pg_stats where HypoPG cannot be had: inferred, graded, refused with the figure.

The catalog is stubbed. Google's advisor cannot be tried here at all: there is
no Cloud SQL or AlloyDB to run it on, so its call is stubbed too.
"""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import QueryError
from pg_debug.index.heuristic import COLUMN_STATS_SQL
from pg_debug.index.heuristic import GOOGLE_DB_ADVISOR_SQL
from pg_debug.index.heuristic import TABLE_ROWS_SQL
from pg_debug.index.heuristic import UNCOSTED_NOTE
from pg_debug.index.heuristic import StatisticsIndexAdvisor
from pg_debug.index.heuristic import _ColumnStats
from pg_debug.index.heuristic import _rows_after
from pg_debug.severity import Confidence
from pg_debug.severity import Severity

ROWS = 1_000_000.0

#: orders: customer_id has 50,000 values, status 3, created_at is unique,
#: note is never analyzed.
STATS = {
    "customer_id": {"null_frac": 0.0, "n_distinct": 50_000.0},
    "status": {"null_frac": 0.0, "n_distinct": 3.0},
    "created_at": {"null_frac": 0.0, "n_distinct": -1.0},
    "tiny_id": {"null_frac": 0.0, "n_distinct": -1.0},
}
COLUMNS = ["id", "customer_id", "status", "created_at", "note", "tiny_id"]


def row(**cells: Any) -> SqlDriver.RowResult:
    return SqlDriver.RowResult(cells=cells)


def catalog(table_rows: float = ROWS, indexes: list[dict[str, Any]] | None = None, google: Any = None, extensions: tuple[str, ...] = ()):
    async def execute_query(query: str, *args: Any, **kwargs: Any) -> list[SqlDriver.RowResult] | None:
        if "FROM pg_attribute" in query:
            rows = [row(schema="public", table="orders", column=c, type="integer") for c in COLUMNS]
            return [*rows, row(schema="public", table="tiny", column="id", type="integer")]
        if "pg_partition_tree" in query:
            return [row(schema="public", table="orders", table_rows=table_rows), row(schema="public", table="tiny", table_rows=100.0)]
        if "FROM pg_stats" in query:
            return [row(schema="public", table="orders", column=c, inherited=False, **s) for c, s in STATS.items()]
        if "FROM pg_index i" in query:
            return [row(**index) for index in indexes or []]
        if "FROM pg_extension" in query:
            return [row(extversion="1.0")] if any(f"'{name}'" in query for name in extensions) else []
        if "pg_available_extensions" in query:
            return []
        if "google_db_advisor_recommend_indexes" in query:
            if isinstance(google, Exception):
                raise google
            return google
        raise AssertionError(f"unexpected query: {query}")

    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(side_effect=execute_query)
    return driver


def btree(name: str, *columns: str, predicate: str | None = None) -> dict[str, Any]:
    return {
        "schema": "public",
        "table": "orders",
        "index": name,
        "method": "btree",
        "valid": True,
        "ready": True,
        "key_columns": len(columns),
        "columns": list(columns),
        "predicate": predicate,
    }


def _restricted_mode_checks_functions_in_from() -> bool:
    try:
        SafeSqlDriver(MagicMock(spec=SqlDriver))._validate("SELECT * FROM pg_terminate_backend(1)")
    except QueryError:
        return True
    return False


#: Restricted mode checked a function called in FROM only from the fix on
#: fix/restricted-nested-calls; before it, these two would pass for the wrong reason or fail.
needs_from_clause_checks = pytest.mark.skipif(
    not _restricted_mode_checks_functions_in_from(),
    reason="restricted mode does not yet check functions called in FROM (fixed on fix/restricted-nested-calls)",
)


async def advise(*queries: str, **kwargs: Any):
    return await StatisticsIndexAdvisor(catalog(**kwargs)).advise(list(queries), "HypoPG is not offered here.")


@pytest.mark.asyncio
async def test_the_most_selective_column_leads_and_columns_stop_once_about_50_rows_are_left():
    advice = await advise("SELECT * FROM orders WHERE status = 'paid' AND customer_id = 42")

    [candidate] = advice.candidates
    # customer_id leaves 20 rows on its own, so status is not worth adding.
    assert candidate.columns == ["customer_id"]
    assert candidate.expected_rows == pytest.approx(20.0)
    assert candidate.confidence is Confidence.CANDIDATE
    assert candidate.severity is Severity.HIGH
    assert candidate.definition == "CREATE INDEX CONCURRENTLY ON public.orders (customer_id)"
    assert "EXPLAIN" in candidate.verify
    assert candidate.to_dict()["costed"] is False


@pytest.mark.asyncio
async def test_a_range_ends_the_index_and_equality_columns_come_first():
    advice = await advise("SELECT * FROM orders WHERE created_at > now() - interval '1 day' AND status = 'paid'")

    [candidate] = advice.candidates
    assert candidate.columns == ["status", "created_at"]
    assert [s.operator for s in candidate.steps] == ["=", ">"]


@pytest.mark.asyncio
async def test_what_an_index_cannot_serve_is_left_out_and_said():
    advice = await advise("SELECT * FROM orders WHERE customer_id = 42 AND lower(note) = 'x' AND status <> 'void'")

    [candidate] = advice.candidates
    assert candidate.columns == ["customer_id"]
    assert any("expression" in note for note in candidate.notes)
    assert any("<>" in note for note in candidate.notes)


@pytest.mark.asyncio
async def test_an_existing_index_that_already_narrows_as_far_refuses_the_candidate():
    advice = await advise("SELECT * FROM orders WHERE customer_id = 42", indexes=[btree("orders_customer_status", "customer_id", "status")])

    assert advice.candidates == []
    [refused] = advice.refused
    assert refused["existing_index"] == "orders_customer_status"
    assert "already serves it" in refused["reason"]


@pytest.mark.asyncio
async def test_a_partial_index_does_not_count_as_covering():
    advice = await advise(
        "SELECT * FROM orders WHERE customer_id = 42",
        indexes=[btree("orders_customer_paid", "customer_id", predicate="status = 'paid'")],
    )
    assert [c.columns for c in advice.candidates] == [["customer_id"]]


@pytest.mark.asyncio
async def test_too_small_a_table_and_too_weak_a_filter_are_refused_with_the_figure():
    advice = await advise("SELECT * FROM tiny WHERE id = 1", "SELECT * FROM orders WHERE status = 'paid'")

    assert advice.candidates == []
    reasons = {r["table"]: r for r in advice.refused}
    assert reasons["public.tiny"]["table_rows"] == 100.0
    assert "a sequential scan" in reasons["public.tiny"]["reason"]
    # One status of three leaves a third of the table.
    assert reasons["public.orders"]["expected_rows"] == pytest.approx(ROWS / 3, rel=0.01)
    assert "33%" in reasons["public.orders"]["reason"]


@pytest.mark.asyncio
async def test_a_column_without_statistics_and_an_unanalyzed_table_are_refused():
    advice = await advise("SELECT * FROM orders WHERE note = 'x'")
    assert advice.candidates == []
    assert "no statistics in pg_stats" in advice.refused[0]["skipped"][0]

    never = await advise("SELECT * FROM orders WHERE customer_id = 1", table_rows=-1.0)
    assert "never been analyzed" in never.refused[0]["reason"]


@pytest.mark.asyncio
async def test_the_same_candidate_from_two_queries_is_one_candidate():
    advice = await advise("SELECT * FROM orders WHERE customer_id = 1", "SELECT id FROM orders WHERE customer_id = 2")
    [candidate] = advice.candidates
    assert len(candidate.queries) == 2


@pytest.mark.asyncio
async def test_the_result_says_it_was_not_costed():
    result = (await advise("SELECT * FROM orders WHERE customer_id = 1")).to_dict()
    assert result["costed"] is False
    assert result["note"] == UNCOSTED_NOTE
    assert "not costed by the planner" in result["note"]
    assert result["hypopg"] == "HypoPG is not offered here."
    assert "google_db_advisor" not in result


def test_pghero_estimates_rows_from_nulls_and_distinct_values():
    assert _rows_after(_ColumnStats(0.5, 10.0), 1000.0, 1000.0, "=") == 50.0
    # A negative n_distinct is a share of the rows: unique leaves one.
    assert _rows_after(_ColumnStats(0.0, -1.0), 1000.0, 1000.0, "=") == 1.0
    assert _rows_after(_ColumnStats(0.2, 10.0), 1000.0, 1000.0, "IS NULL") == 200.0
    # A range keeps (rows + rows per value) / 10.
    assert _rows_after(_ColumnStats(0.0, 10.0), 1000.0, 1000.0, ">") == 110.0


# -- Google's advisor, stubbed ------------------------------------------------


@pytest.mark.asyncio
async def test_google_db_advisor_is_read_where_it_is_installed():
    recommendations = [row(index="CREATE INDEX ON public.orders(customer_id)", estimated_storage_size_in_mb=12)]
    result = (await advise("SELECT * FROM orders WHERE customer_id = 1", extensions=("google_db_advisor",), google=recommendations)).to_dict()

    google = result["google_db_advisor"]
    assert google["recommendations"] == [{"index": "CREATE INDEX ON public.orders(customer_id)", "estimated_storage_size_in_mb": 12}]
    assert "not for the queries given" in google["note"]


@pytest.mark.asyncio
async def test_google_db_advisor_that_fails_says_why():
    result = (await advise("SELECT 1", extensions=("google_db_advisor",), google=QueryError("permission denied"))).to_dict()
    assert result["google_db_advisor"]["error"] == "google_db_advisor_recommend_indexes() could not be run: permission denied"


@needs_from_clause_checks
@pytest.mark.asyncio
async def test_restricted_mode_refuses_google_db_advisor_and_the_result_says_so():
    """pg-debug cannot confirm the function writes nothing, so it is not allowlisted."""
    inner = catalog(extensions=("google_db_advisor",), google=[])
    safe = SafeSqlDriver(inner)
    result = (await StatisticsIndexAdvisor(safe).advise(["SELECT 1"], "no hypopg")).to_dict()
    assert "could not be run" in result["google_db_advisor"]["error"]


def test_the_catalog_reads_pass_restricted_mode():
    safe = SafeSqlDriver(MagicMock(spec=SqlDriver))
    safe._validate(SafeSqlDriver.param_sql_to_query(TABLE_ROWS_SQL, [["public.orders"]]))
    safe._validate(SafeSqlDriver.param_sql_to_query(COLUMN_STATS_SQL, [["public.orders"], ["customer_id"]]))


@needs_from_clause_checks
def test_restricted_mode_does_not_allow_google_db_advisor():
    with pytest.raises(QueryError):
        SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(GOOGLE_DB_ADVISOR_SQL)
