"""The index advisor's checks against a real planner, and its advice where HypoPG cannot be had.

HypoPG is installed on the test servers, so "cannot be had" is staged by
patching the extension check; everything after it reads the real catalog.
"""

from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from pglast import parse_sql

from pg_debug import PgDebug
from pg_debug.db import SqlDriver
from pg_debug.db import extensions
from pg_debug.db.extensions import ExtensionStatus
from pg_debug.index.base import IndexRecommendation
from pg_debug.index.base import is_hypothetical_name_of
from pg_debug.index.dta import DatabaseTuningAdvisor
from pg_debug.plans.hypothetical import IndexDefinition

TABLE = "pgdebug_advice_orders"


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


@pytest.fixture
def orders(url):
    """200,000 orders: 5,000 customers, three statuses, 300 days, analyzed.

    Each row carries 200 bytes besides, as real rows do: on a table no wider
    than its indexes, the search's size term outweighs what they save.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        for extension in ("hypopg", "pg_stat_statements"):
            try:
                conn.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            except psycopg.Error:
                pytest.skip(f"{extension} is not available")
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(f"CREATE TABLE {TABLE} (id int PRIMARY KEY, customer_id int, status text, created date, amount int, note text)")
        conn.execute(
            f"INSERT INTO {TABLE} SELECT i, i % 5000, (ARRAY['new', 'paid', 'void'])[i % 3 + 1], "
            "date '2026-01-01' + (i % 300), i % 977, repeat('x', 200) FROM generate_series(1, 200000) i"
        )
        conn.execute(f"ANALYZE {TABLE}")
    try:
        yield TABLE
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")


def weights(*queries: str) -> list[tuple[str, Any, float]]:
    return [(q, parse_sql(q)[0].stmt, 1.0) for q in queries]


@pytest.mark.asyncio
async def test_each_index_counts_the_queries_whose_plan_reads_it(url, orders):
    by_customer = f"select * from {orders} where customer_id = 42"
    by_amount = f"select * from {orders} where amount = 5 and status = 'paid'"
    # Already served by the primary key, and cheap: no recommended index is for it.
    unhelped = f"select * from {orders} where id = 5"
    async with PgDebug(url) as pg:
        result = await pg.analyze_query_indexes([by_customer, by_amount, unhelped])

    recommendations = {tuple(rec["index_target_columns"]): rec for rec in result["recommendations"]}
    customer = next(rec for cols, rec in recommendations.items() if cols[0] == "customer_id")
    assert customer["queries_using"] == [by_customer]
    assert customer["queries_using_index"] == 1
    assert customer["workload_queries"] == 3
    assert all(unhelped not in rec["queries_using"] for rec in recommendations.values())

    check = result["regression_check"]
    assert check["queries_checked"] == 3
    assert check["max_regression_factor"] == 0.1
    assert check["refused"] == []
    assert check["worst_query"]["ratio"] <= 1.1

    assert result["workload"]["source"] == "query_list"
    assert "deterministic" in result["workload"]["note"]


@pytest.mark.asyncio
async def test_hypopgs_name_for_an_index_is_the_one_the_plan_shows(url, orders):
    driver = SqlDriver(engine_url=url)
    advisor = DatabaseTuningAdvisor(driver)
    index = IndexDefinition(orders, ("customer_id", "status"))
    plan = await advisor.get_explain_plan_with_indexes(f"select * from {orders} where customer_id = 1 and status = 'paid'", frozenset({index}))
    names = [node.get("Index Name") for node in [plan["Plan"], *plan["Plan"].get("Plans", [])] if node.get("Index Name")]
    assert names and is_hypothetical_name_of(names[0], index)


@pytest.mark.asyncio
async def test_an_index_led_by_anothers_columns_is_merged_into_it(url, orders):
    driver = SqlDriver(engine_url=url)
    advisor = DatabaseTuningAdvisor(driver)
    one = IndexRecommendation(orders, ("customer_id",))
    two = IndexRecommendation(orders, ("customer_id", "status"))
    queries = weights(f"select * from {orders} where customer_id = 42", f"select * from {orders} where customer_id = 42 and status = 'paid'")

    kept = await advisor._consolidate(queries, [one, two])

    assert kept == [two]
    [merge] = advisor._consolidations
    assert merge["merged"] is True and merge["into"] == two.definition
    assert merge["worst_ratio"] <= 1.1
    # Measured by HypoPG: one index of two columns is smaller than both together.
    assert 0 < merge["size_after_bytes"] < merge["size_before_bytes"]


@pytest.mark.asyncio
async def test_a_workload_from_pg_stat_statements_carries_its_counting_window(url, orders):
    with psycopg.connect(url, autocommit=True) as conn:
        version = conn.info.server_version // 10000
        try:
            conn.execute("SELECT pg_stat_statements_reset()")
        except psycopg.Error:
            pytest.skip("pg_stat_statements is not loaded")
        for i in range(60):
            conn.execute(f"SELECT * FROM {TABLE} WHERE customer_id = %s AND amount >= 0", [i])

    async with PgDebug(url) as pg:
        result = await pg.analyze_workload_indexes()

    workload = result["workload"]
    assert workload["source"] == "query_store"
    window = workload["counting_window"]
    if version >= 14:
        assert window["counting_since"] is not None
        assert window["counting_began_with"] == "reset"
        assert window["counting_seconds"] < 3600
    else:
        assert window["counting_since"] is None
    assert "pg_stat_statements" in workload["note"]


# -- Without HypoPG -------------------------------------------------------------


def hypopg_unavailable():
    """HypoPG neither installed nor listed; every other extension as the server says."""
    real = extensions.check_extension

    async def check(sql_driver, name, *args, **kwargs):
        if name == "hypopg":
            return ExtensionStatus(is_installed=False, is_available=False, name="hypopg", message="", default_version=None)
        return await real(sql_driver, name, *args, **kwargs)

    return patch("pg_debug.db.extensions.check_extension", check)


@pytest.mark.asyncio
async def test_without_hypopg_tune_queries_returns_graded_candidates_from_the_statistics(url, orders):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {TABLE}_status_idx ON {TABLE} (status)")
    by_customer = f"SELECT * FROM {orders} WHERE status = 'paid' AND customer_id = 42"
    by_status = f"SELECT * FROM {orders} WHERE status = 'void'"
    by_range = f"SELECT * FROM {orders} WHERE created > date '2026-10-20' AND amount = 5"

    with hypopg_unavailable():
        async with PgDebug(url) as pg:
            result = await pg.analyze_query_indexes([by_customer, by_status, by_range])

    assert result["costed"] is False
    assert "not costed by the planner" in result["note"]
    assert "hypopg" in result["hypopg"].lower()

    candidates = {tuple(c["columns"]): c for c in result["candidates"]}
    # customer_id leaves about 40 rows of 200,000 on its own, so status is not added.
    customer = candidates[("customer_id",)]
    assert customer["confidence"] == "candidate"
    assert customer["severity"] in ("high", "medium")
    assert customer["queries"] == [by_customer]
    assert 10 < customer["expected_rows"] < 100
    assert customer["definition"] == f"CREATE INDEX CONCURRENTLY ON public.{orders} (customer_id)"
    # Equality first, the range last.
    assert ("amount", "created") in candidates

    refused = {r["query"]: r for r in result["refused"]}
    # A third of the table is not worth an index, and one exists on it anyway.
    assert by_status in refused
    assert "google_db_advisor" not in result


@pytest.mark.asyncio
async def test_without_hypopg_restricted_mode_still_returns_candidates(url, orders):
    with hypopg_unavailable():
        async with PgDebug(url, access_mode="restricted") as pg:
            result = await pg.analyze_query_indexes([f"SELECT * FROM {orders} WHERE customer_id = 7"])
    assert [c["columns"] for c in result["candidates"]] == [["customer_id"]]
