"""End-to-end tests for the client and CLI against a real PostgreSQL server.

These are skipped unless PG_DEBUG_TEST_DATABASE_URI points at a database the
test may create and drop tables in. Unlike the other integration tests they do
not need Docker, so they can run against any server you already have:

    PG_DEBUG_TEST_DATABASE_URI=postgresql://postgres@localhost:5432/postgres \\
        uv run pytest tests/integration/test_client_integration.py
"""

import asyncio
import json
import os

import pytest
import pytest_asyncio

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import cli
from pg_debug.artifacts import ExplainPlanArtifact
from pg_debug.errors import ExtensionRequiredError
from pg_debug.errors import InvalidArgumentError
from pg_debug.index.dta_calc import HYPOTHETICAL_INDEX_SIZES_SQL
from pg_debug.sql import SafeSqlDriver

DATABASE_URI = os.environ.get("PG_DEBUG_TEST_DATABASE_URI")

pytestmark = pytest.mark.skipif(not DATABASE_URI, reason="PG_DEBUG_TEST_DATABASE_URI is not set")

SCHEMA = "pgdebug_test"


@pytest_asyncio.fixture
async def pg():
    """A client against a throwaway schema with a small orders/customers model."""
    client = PgDebug(DATABASE_URI)
    await client.execute_sql(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    await client.execute_sql(f"CREATE SCHEMA {SCHEMA}")
    await client.execute_sql(
        f"""
        CREATE TABLE {SCHEMA}.customers (
            id serial PRIMARY KEY,
            email text UNIQUE
        )
        """
    )
    await client.execute_sql(
        f"""
        CREATE TABLE {SCHEMA}.orders (
            id serial PRIMARY KEY,
            customer_id integer REFERENCES {SCHEMA}.customers(id),
            total numeric NOT NULL CHECK (total >= 0),
            status text NOT NULL
        )
        """
    )
    await client.execute_sql(f"CREATE VIEW {SCHEMA}.paid_orders AS SELECT * FROM {SCHEMA}.orders WHERE status = 'paid'")
    await client.execute_sql(f"INSERT INTO {SCHEMA}.customers (email) SELECT 'c' || i FROM generate_series(1, 500) i")
    await client.execute_sql(
        f"""
        INSERT INTO {SCHEMA}.orders (customer_id, total, status)
        SELECT (i % 500) + 1, (i % 97)::numeric, CASE WHEN i %% 3 = 0 THEN 'paid' ELSE 'open' END
        FROM generate_series(1, 5000) i
        """.replace("%%", "%")
    )
    await client.execute_sql(f"ANALYZE {SCHEMA}.orders")

    try:
        yield client
    finally:
        await client.execute_sql(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await client.close()


async def has_extension(pg: PgDebug, name: str) -> bool:
    return bool(await pg.execute_sql(f"SELECT 1 FROM pg_extension WHERE extname = '{name}'"))


@pytest.mark.asyncio
async def test_list_schemas_includes_the_test_schema(pg):
    names = {row["schema_name"] for row in await pg.list_schemas()}
    assert SCHEMA in names
    assert "pg_catalog" in names


@pytest.mark.asyncio
async def test_list_objects_by_type(pg):
    tables = {row["name"] for row in await pg.list_objects(SCHEMA)}
    views = {row["name"] for row in await pg.list_objects(SCHEMA, "view")}
    sequences = {row["name"] for row in await pg.list_objects(SCHEMA, "sequence")}

    assert tables == {"customers", "orders"}
    assert views == {"paid_orders"}
    assert "orders_id_seq" in sequences


@pytest.mark.asyncio
async def test_get_object_details(pg):
    details = await pg.get_object_details(SCHEMA, "orders")

    assert details["basic"] == {"schema": SCHEMA, "name": "orders", "type": "table"}
    assert [c["column"] for c in details["columns"]] == ["id", "customer_id", "total", "status"]
    assert {c["type"] for c in details["constraints"]} >= {"PRIMARY KEY", "FOREIGN KEY", "CHECK"}
    assert any(idx["name"] == "orders_pkey" for idx in details["indexes"])


@pytest.mark.asyncio
async def test_get_object_details_for_unknown_object(pg):
    assert await pg.get_object_details(SCHEMA, "not_a_table", "sequence") == {}


@pytest.mark.asyncio
async def test_execute_sql_returns_rows(pg):
    rows = await pg.execute_sql(f"SELECT status, count(*) AS n FROM {SCHEMA}.orders GROUP BY status ORDER BY status")
    assert [row["status"] for row in rows] == ["open", "paid"]
    assert sum(row["n"] for row in rows) == 5000


@pytest.mark.asyncio
async def test_explain_query(pg):
    plan = await pg.explain_query(f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 42")

    assert isinstance(plan, ExplainPlanArtifact)
    assert plan.plan_tree.total_cost > 0
    assert json.loads(plan.value)["Plan"]["Node Type"]


@pytest.mark.asyncio
async def test_explain_analyze_reports_execution_time(pg):
    plan = await pg.explain_query(f"SELECT count(*) FROM {SCHEMA}.orders", analyze=True)

    assert plan.execution_time is not None
    assert plan.plan_tree.actual_rows is not None


@pytest.mark.asyncio
async def test_explain_with_hypothetical_index(pg):
    indexes = [{"table": f"{SCHEMA}.orders", "columns": ["customer_id"]}]

    if not await has_extension(pg, "hypopg"):
        with pytest.raises(ExtensionRequiredError):
            await pg.explain_query(f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 42", hypothetical_indexes=indexes)
        return

    without = await pg.explain_query(f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 42")
    with_index = await pg.explain_query(f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 42", hypothetical_indexes=indexes)

    assert with_index.plan_tree.total_cost < without.plan_tree.total_cost


@pytest.mark.asyncio
async def test_restricted_mode_rejects_writes():
    async with PgDebug(DATABASE_URI, access_mode=AccessMode.RESTRICTED) as pg:
        assert await pg.execute_sql("SELECT 1 AS ok") == [{"ok": 1}]

        with pytest.raises(Exception, match=r"[Ee]rror validating query"):
            await pg.execute_sql("CREATE TABLE should_not_exist (id int)")


@pytest.mark.asyncio
async def test_analyze_db_health(pg):
    results = await pg.analyze_db_health("index,connection,buffer,constraint")

    assert set(results) == {
        "Invalid index check",
        "Duplicate index check",
        "Index bloat",
        "Unused index check",
        "Connection health",
        "Buffer health for indexes",
        "Buffer health for tables",
        "Constraint health",
    }
    assert all(check.name == name and check.summary for name, check in results.items())
    assert results["Connection health"].metrics["total_connections"] >= 1
    # A healthy check reports nothing to act on.
    assert all(not check.findings for check in results.values() if check.healthy)


@pytest.mark.asyncio
async def test_analyze_db_health_flags_a_duplicate_index(pg, tuning_table):
    await pg.execute_sql(f"CREATE INDEX {tuning_table}_status_idx ON {tuning_table} (status)")
    await pg.execute_sql(f"CREATE INDEX {tuning_table}_status_dup_idx ON {tuning_table} (status)")

    results = await pg.analyze_db_health("index")

    pair = {f"{tuning_table}_status_idx", f"{tuning_table}_status_dup_idx"}

    # The check is database-wide, so look only at the pair this test created.
    # Either one may be reported as redundant; the other is its covering index.
    duplicates = results["Duplicate index check"]
    assert duplicates.healthy is False
    ours = [f for f in duplicates.findings if f["table"] == tuning_table]
    assert len(ours) == 1
    assert pair == {ours[0]["index"], ours[0]["covered_by"]}

    unused = results["Unused index check"]
    assert unused.healthy is False
    assert {f["index"] for f in unused.findings} >= pair
    assert all(f["index_scans"] == 0 for f in unused.findings if f["index"] in pair)


def test_cli_health_exit_code_against_a_real_database(capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")

    # Connection health on an idle test database is healthy.
    assert cli.main(["health", "--type", "connection", "--exit-code"]) == 0
    assert "Connection health:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_analyze_db_health_rejects_unknown_check(pg):
    with pytest.raises(InvalidArgumentError):
        await pg.analyze_db_health("disk")


@pytest.mark.asyncio
async def test_get_top_queries(pg):
    if not await has_extension(pg, "pg_stat_statements"):
        with pytest.raises(ExtensionRequiredError):
            await pg.get_top_queries(sort_by="total_time")
        return

    rows = await pg.get_top_queries(sort_by="total_time", limit=5)

    assert len(rows) <= 5
    assert all("query" in row and "calls" in row for row in rows)


@pytest_asyncio.fixture
async def tuning_table(pg):
    """A table in the default schema.

    The index advisor resolves table names through the connection's
    search_path, so a table in an unrelated schema would yield no candidates.
    """
    table = "pgdebug_test_orders"
    await pg.execute_sql(f"DROP TABLE IF EXISTS {table}")
    await pg.execute_sql(f"CREATE TABLE {table} (id serial PRIMARY KEY, customer_id integer, status text)")
    await pg.execute_sql(f"INSERT INTO {table} (customer_id, status) SELECT i % 500, 'open' FROM generate_series(1, 5000) i")
    await pg.execute_sql(f"ANALYZE {table}")
    try:
        yield table
    finally:
        await pg.execute_sql(f"DROP TABLE IF EXISTS {table}")


@pytest.mark.asyncio
async def test_analyze_query_indexes(pg, tuning_table):
    query = f"SELECT * FROM {tuning_table} WHERE customer_id = 42 AND status = 'paid'"

    result = await pg.analyze_query_indexes([query])

    if not await has_extension(pg, "hypopg"):
        assert "hypopg" in result["error"]
        return

    assert result["summary"]["total_recommendations"] >= 1
    assert all(rec["index_target_table"] == tuning_table for rec in result["recommendations"])
    assert all(rec["index_definition"].startswith("CREATE INDEX pg_debug_idx_") for rec in result["recommendations"])


@pytest.mark.asyncio
async def test_hypothetical_index_sizes_are_measured_in_one_round_trip(pg):
    """Creating and measuring must share a statement, and avoid hypopg's views.

    Hypothetical indexes live in the session that created them, and the pool
    does not promise the next statement the same connection — so measuring in a
    separate call can see none of them. The lookup also reads the hypopg()
    function rather than hypopg_list_indexes, which HypoPG grants no privileges
    on, so any role that did not create the extension gets "permission denied
    for view".
    """
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    create_and_measure = (
        SafeSqlDriver.param_sql_to_query(
            "SELECT hypopg_create_index({});",
            [f"CREATE INDEX ON {SCHEMA}.orders (customer_id)"],
        )
        + HYPOTHETICAL_INDEX_SIZES_SQL
    )

    try:
        rows = await pg.execute_sql(create_and_measure)

        assert len(rows) == 1
        assert rows[0]["index_size"] > 0
        assert "customer_id" in rows[0]["index_name"]
    finally:
        await pg.execute_sql("SELECT hypopg_reset()")


@pytest.mark.asyncio
async def test_index_recommendations_carry_a_measured_size(pg, tuning_table):
    """Sizes must survive a pool that has opened more than one connection."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    # Churn the pool so later statements are not guaranteed the same session.
    for _ in range(4):
        await pg.execute_sql("SELECT pg_backend_pid()")

    result = await pg.analyze_query_indexes([f"SELECT * FROM {tuning_table} WHERE customer_id = 42 AND status = 'paid'"])

    assert result["recommendations"], "expected at least one recommendation"
    assert all(rec["index_estimated_size"] not in ("0 Bytes", None) for rec in result["recommendations"])


@pytest.mark.asyncio
async def test_analyze_query_indexes_rejects_too_many_queries(pg):
    with pytest.raises(InvalidArgumentError):
        await pg.analyze_query_indexes([f"SELECT {i}" for i in range(11)])


@pytest.mark.asyncio
async def test_analyze_query_indexes_for_a_schema_qualified_table(pg):
    """The advisor resolves a qualified table without help from the search_path."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    result = await pg.analyze_query_indexes([f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 42 AND status = 'paid'"])

    assert result["summary"]["total_recommendations"] >= 1
    assert all(rec["index_target_table"] == f"{SCHEMA}.orders" for rec in result["recommendations"])
    assert all(f" ON {SCHEMA}.orders " in rec["index_definition"] for rec in result["recommendations"])


@pytest.mark.asyncio
async def test_explain_with_a_hypothetical_index_on_a_qualified_table(pg):
    """The generated index name stays a valid identifier for a qualified table."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    plan = await pg.explain_query(
        f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 42",
        hypothetical_indexes=[{"table": f"{SCHEMA}.orders", "columns": ["customer_id"]}],
    )

    assert "Index Scan" in plan.to_text()


@pytest.mark.asyncio
async def test_list_activity_sees_a_running_query(pg):
    """A second connection running a slow query shows up as active."""
    async with PgDebug(DATABASE_URI) as other:
        slow = asyncio.create_task(other.execute_sql("SELECT pg_sleep(3)"))
        try:
            await asyncio.sleep(0.5)
            rows = await pg.list_activity()
            sleeping = [r for r in rows if "pg_sleep" in (r["query"] or "")]

            assert sleeping, "the sleeping backend should be reported"
            assert sleeping[0]["state"] == "active"
            assert sleeping[0]["blocked_by"] == []
            assert sleeping[0]["state_seconds"] > 0
        finally:
            await slow


@pytest.mark.asyncio
async def test_list_activity_excludes_the_calling_backend(pg):
    for row in await pg.list_activity(include_idle=True):
        assert "pg_stat_activity" not in (row["query"] or ""), "our own query should be excluded"


@pytest.mark.asyncio
async def test_list_activity_reports_who_is_blocking_whom(pg):
    """One session holds a lock while another waits for it.

    The lock is taken and released inside a single statement: the driver
    commits after every statement and the pool may hand the next one a
    different connection, so no session state can be held across calls. A
    transaction-scoped advisory lock releases itself on that commit, which also
    means neither task can outlive the test.
    """
    lock_id = 8811_2026

    async with PgDebug(DATABASE_URI) as holder, PgDebug(DATABASE_URI) as waiter:
        held = asyncio.create_task(holder.execute_sql(f"SELECT pg_advisory_xact_lock({lock_id}), pg_sleep(3)"))
        await asyncio.sleep(0.5)
        blocked = asyncio.create_task(waiter.execute_sql(f"SELECT pg_advisory_xact_lock({lock_id})"))

        try:
            await asyncio.sleep(1)

            waiting = await pg.list_activity(blocked_only=True)
            assert len(waiting) == 1
            assert waiting[0]["wait_event_type"] == "Lock"
            assert waiting[0]["blocked_by"], "the blocking pid should be reported"
        finally:
            await asyncio.gather(held, blocked)


@pytest.mark.asyncio
async def test_list_activity_min_duration_filters(pg):
    assert await pg.list_activity(include_idle=True, min_duration_seconds=100_000) == []


@pytest.mark.asyncio
async def test_vacuum_progress_when_nothing_is_vacuuming(pg):
    rows = await pg.vacuum_progress()
    assert all("phase" in row for row in rows)


@pytest.mark.asyncio
async def test_column_stats_reflect_the_data(pg):
    stats = {row["column"]: row for row in await pg.column_stats(f"{SCHEMA}.orders")}

    assert set(stats) >= {"id", "customer_id", "status"}
    # 500 customers over 5000 rows, and the id column is unique.
    assert stats["customer_id"]["n_distinct"] > 0
    assert stats["id"]["n_distinct"] == -1
    assert stats["status"]["null_frac"] == 0


@pytest.mark.asyncio
async def test_column_stats_for_a_table_without_statistics(pg):
    await pg.execute_sql(f"CREATE TABLE {SCHEMA}.unanalyzed (id int)")
    await pg.execute_sql(f"INSERT INTO {SCHEMA}.unanalyzed SELECT generate_series(1, 100)")

    assert await pg.column_stats(f"{SCHEMA}.unanalyzed") == []


@pytest.mark.asyncio
async def test_extended_statistics_round_trip(pg):
    assert await pg.extended_statistics(f"{SCHEMA}.orders") == []

    await pg.execute_sql(f"CREATE STATISTICS {SCHEMA}.orders_customer_status ON customer_id, status FROM {SCHEMA}.orders")

    (stats,) = await pg.extended_statistics(f"{SCHEMA}.orders")
    assert stats["name"] == "orders_customer_status"
    assert sorted(stats["columns"]) == ["customer_id", "status"]


@pytest.mark.asyncio
async def test_statistics_health_flags_a_never_analyzed_table(pg):
    await pg.execute_sql(f"CREATE TABLE {SCHEMA}.never (id int)")
    await pg.execute_sql(f"ALTER TABLE {SCHEMA}.never SET (autovacuum_enabled = false)")
    await pg.execute_sql(f"INSERT INTO {SCHEMA}.never SELECT generate_series(1, 5000)")

    # Statistics arrive asynchronously; give the collector a moment.
    check = None
    findings = []
    for _ in range(20):
        check = (await pg.analyze_db_health("statistics"))["Statistics health"]
        findings = [f for f in check.findings if f["table"] == "never"]
        if findings:
            break
        await asyncio.sleep(0.25)

    assert findings, "a table with rows and no ANALYZE should be reported"
    assert findings[0]["never_analyzed"] is True
    assert check is not None and check.healthy is False


@pytest.mark.asyncio
async def test_top_queries_sampling_measures_only_the_interval(pg, tuning_table):
    if not await has_extension(pg, "pg_stat_statements"):
        pytest.skip("pg_stat_statements is not installed")

    marker = f"SELECT count(*) FROM {tuning_table} WHERE status = 'sampled'"

    async def keep_running(stop: asyncio.Event):
        async with PgDebug(DATABASE_URI) as worker:
            while not stop.is_set():
                await worker.execute_sql(marker)

    stop = asyncio.Event()
    workload = asyncio.create_task(keep_running(stop))
    try:
        sampled = await pg.get_top_queries(sort_by="total_time", limit=5, interval_seconds=2)
    finally:
        stop.set()
        await workload

    queries = [row["query"] for row in sampled]
    assert any(tuning_table in q for q in queries), "the query running during the sample should be reported"
    assert all("BEGIN" not in q.upper()[:5] for q in queries), "transaction control is noise"
    assert all(row["calls"] > 0 for row in sampled)


@pytest.mark.asyncio
async def test_top_queries_sampling_rejects_a_zero_interval(pg):
    with pytest.raises(InvalidArgumentError):
        await pg.get_top_queries(interval_seconds=0)


def test_cli_against_a_real_database(capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")

    assert cli.main(["--json", "execute", "SELECT 1 AS ok"]) == 0

    assert json.loads(capsys.readouterr().out) == [{"ok": 1}]


def test_cli_reports_connection_failures(capsys):
    assert cli.main(["-d", "postgresql://nobody:secret@127.0.0.1:1/nope", "schemas"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret" not in captured.err
