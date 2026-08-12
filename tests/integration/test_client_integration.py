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
from typing import LiteralString
from typing import cast

import psycopg
import pytest
import pytest_asyncio

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug import SyncPgDebug
from pg_debug import cli
from pg_debug.artifacts import ExplainPlanArtifact
from pg_debug.database_health.autovacuum_calc import AutovacuumCalc
from pg_debug.database_health.table_bloat_calc import TableBloatCalc
from pg_debug.errors import ExtensionRequiredError
from pg_debug.errors import InvalidArgumentError
from pg_debug.index.dta_calc import HYPOTHETICAL_INDEX_SIZES_SQL
from pg_debug.index.dta_calc import DatabaseTuningAdvisor
from pg_debug.index.index_opt_base import HIDDEN_QUERY_TEXT
from pg_debug.partition import Strategy
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
async def test_explain_with_an_expression_index(pg):
    """An expression index has to survive parsing, naming and the server."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    query = f"SELECT * FROM {SCHEMA}.customers WHERE lower(email) = 'c42'"
    indexes = [{"table": f"{SCHEMA}.customers", "columns": ["lower(email)"]}]

    without = await pg.explain_query(query)
    with_index = await pg.explain_query(query, hypothetical_indexes=indexes)

    assert "Seq Scan" in without.to_text()
    assert with_index.plan_tree.total_cost < without.plan_tree.total_cost


@pytest.mark.asyncio
async def test_explain_with_a_multi_argument_expression_index(pg):
    """The commas inside the call must not be read as column separators."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    query = f"SELECT * FROM {SCHEMA}.customers WHERE substr(email, 1, 3) = 'c42'"
    indexes = [{"table": f"{SCHEMA}.customers", "columns": ["substr(email, 1, 3)"]}]

    without = await pg.explain_query(query)
    with_index = await pg.explain_query(query, hypothetical_indexes=indexes)

    assert with_index.plan_tree.total_cost < without.plan_tree.total_cost


def test_cli_explain_with_an_expression_index(capsys, monkeypatch):
    """The whole path: CLI spec parsing, index naming, then the server.

    cli.main() runs its own event loop, so this stays synchronous and sets up
    through SyncPgDebug rather than the async fixture.
    """
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")
    table = "pgdebug_expression_test"

    with SyncPgDebug(DATABASE_URI) as pg:
        if not pg.execute_sql("SELECT 1 FROM pg_extension WHERE extname = 'hypopg'"):
            pytest.skip("hypopg is not installed")

        pg.execute_sql(f"DROP TABLE IF EXISTS {table}")
        pg.execute_sql(f"CREATE TABLE {table} (id serial PRIMARY KEY, email text)")
        pg.execute_sql(f"INSERT INTO {table} (email) SELECT 'C' || i FROM generate_series(1, 5000) i")
        pg.execute_sql(f"ANALYZE {table}")

        try:
            code = cli.main(["explain", f"SELECT * FROM {table} WHERE lower(email) = 'c42'", "-i", f"{table}(lower(email))"])

            captured = capsys.readouterr()
            assert code == 0, captured.err
            assert "Index Scan" in captured.out
        finally:
            pg.execute_sql(f"DROP TABLE IF EXISTS {table}")


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


@pytest.mark.asyncio
async def test_table_bloat_flags_a_churned_table(pg):
    """Delete most of a table without vacuuming it back down and it shows up."""
    table = f"{SCHEMA}.churned"
    # Autovacuum would shrink the dead space back out from under the test.
    await pg.execute_sql(f"CREATE TABLE {table} (id integer, payload text) WITH (autovacuum_enabled = false)")
    await pg.execute_sql(f"INSERT INTO {table} SELECT i, repeat('x', 200) FROM generate_series(1, 40000) i")
    await pg.execute_sql(f"DELETE FROM {table} WHERE id % 10 <> 0")
    # The deleted rows still occupy their pages: relpages does not shrink, and
    # ANALYZE brings reltuples down to the rows that are actually live. That
    # gap is exactly what the check measures.
    await pg.execute_sql(f"ANALYZE {table}")

    calc = TableBloatCalc(await pg.sql_driver())
    check = await calc.table_bloat_check(min_bloat_bytes=1024 * 1024, min_bloat_percent=50.0)

    assert check.healthy is False
    ours = [f for f in check.findings if f["schema"] == SCHEMA and f["table"] == "churned"]
    assert len(ours) == 1
    assert float(ours[0]["bloat_percent"]) >= 50.0
    assert int(ours[0]["bloat_bytes"]) < int(ours[0]["table_bytes"])


@pytest.mark.asyncio
async def test_table_bloat_leaves_a_freshly_loaded_table_alone(pg):
    """The default thresholds must not fire on a table with nothing wrong."""
    table = f"{SCHEMA}.compact"
    await pg.execute_sql(f"CREATE TABLE {table} (id integer, payload text)")
    await pg.execute_sql(f"INSERT INTO {table} SELECT i, repeat('x', 200) FROM generate_series(1, 40000) i")
    await pg.execute_sql(f"ANALYZE {table}")

    check = await TableBloatCalc(await pg.sql_driver()).table_bloat_check()

    assert not [f for f in check.findings if f["table"] == "compact"]


@pytest.mark.asyncio
async def test_table_bloat_accounts_for_alignment_padding(pg):
    """Column order changes the row width, and the estimate has to follow it.

    Every column here is a fixed width, so a correct estimate matches the
    table exactly. Summing the column widths instead of laying them out would
    miss the padding between them and report this table as bloated.
    """
    table = f"{SCHEMA}.padded"
    await pg.execute_sql(
        f"""
        CREATE TABLE {table} (
            a text,          -- short varlena: unaligned
            b timestamptz,   -- forces padding up to an 8-byte boundary
            c integer,
            d bigint,        -- and again
            e boolean,
            f text
        )
        """
    )
    await pg.execute_sql(f"INSERT INTO {table} SELECT repeat('x', 40), now(), i, i, i % 2 = 0, repeat('y', 12) FROM generate_series(1, 40000) i")
    await pg.execute_sql(f"ANALYZE {table}")

    check = await TableBloatCalc(await pg.sql_driver()).table_bloat_check(min_bloat_bytes=0, min_bloat_percent=0.0)

    assert not [f for f in check.findings if f["table"] == "padded"], "a table with no dead space was reported as bloated"


@pytest.mark.asyncio
async def test_table_bloat_respects_a_non_default_fillfactor(pg):
    """Deliberately leaving 30% of each page free is not 30% bloat."""
    table = f"{SCHEMA}.roomy"
    await pg.execute_sql(f"CREATE TABLE {table} (a bigint, b timestamptz, c integer) WITH (fillfactor = 70)")
    await pg.execute_sql(f"INSERT INTO {table} SELECT i, now(), i FROM generate_series(1, 60000) i")
    await pg.execute_sql(f"ANALYZE {table}")

    check = await TableBloatCalc(await pg.sql_driver()).table_bloat_check(min_bloat_bytes=0, min_bloat_percent=0.0)

    assert not [f for f in check.findings if f["table"] == "roomy"]


@pytest.mark.asyncio
async def test_table_bloat_check_runs_in_restricted_mode(pg):
    """The estimate is a plain SELECT, so it has to pass the safe-SQL parser."""
    restricted = PgDebug(DATABASE_URI, access_mode="restricted")
    try:
        check = await TableBloatCalc(await restricted.sql_driver()).table_bloat_check()
        assert check.name == "Table bloat"
        assert check.summary
    finally:
        await restricted.close()


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
async def test_a_session_keeps_hypothetical_indexes_alive_across_statements(pg):
    """The point of pinning: session state survives separate execute_query calls."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    driver = await pg.sql_driver()
    create = SafeSqlDriver.param_sql_to_query(
        "SELECT hypopg_create_index({});",
        [f"CREATE INDEX ON {SCHEMA}.orders (customer_id)"],
    )

    try:
        async with driver.session():
            pids = set()
            for _ in range(3):
                rows = await driver.execute_query("SELECT pg_backend_pid() AS pid")
                assert rows
                pids.add(rows[0].cells["pid"])
            assert len(pids) == 1, "a session must not change connections"

            await driver.execute_query(create)  # type: ignore[arg-type]
            visible = await driver.execute_query(HYPOTHETICAL_INDEX_SIZES_SQL)

            assert visible, "the hypothetical index vanished inside a session"
            assert visible[0].cells["index_size"] > 0
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
        # A plain connection, not the client: everything pg_debug runs is
        # marked as its own and excluded from its own reports, so driving the
        # workload through it would measure nothing.
        async with await psycopg.AsyncConnection.connect(DATABASE_URI or "") as worker:
            while not stop.is_set():
                await worker.execute(cast(LiteralString, marker))

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


# -- Data model review ------------------------------------------------------

MODEL_SCHEMA = "pgdebug_model_test"
CLEAN_SCHEMA = "pgdebug_model_clean"


@pytest_asyncio.fixture
async def model_pg():
    """Two schemas: one with exactly one instance of each defect, one with none.

    The clean schema is the more important half. A rule that fires on a
    well-built model is worse than no rule at all, so every rule has to stay
    silent there while finding its own target next door.
    """
    client = PgDebug(DATABASE_URI)
    for schema in (MODEL_SCHEMA, CLEAN_SCHEMA):
        await client.execute_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await client.execute_sql(f"CREATE SCHEMA {schema}")

    # The flawed model.
    await client.execute_sql(f"CREATE TABLE {MODEL_SCHEMA}.parent (id bigint PRIMARY KEY, code text, UNIQUE (id, code))")
    await client.execute_sql(f"INSERT INTO {MODEL_SCHEMA}.parent SELECT i, 'code' || i FROM generate_series(1, 1000) i")

    # unindexed_foreign_key, and the four shapes that must not be flagged.
    await client.execute_sql(f"CREATE TABLE {MODEL_SCHEMA}.fk_unindexed (id int PRIMARY KEY, parent_id bigint REFERENCES {MODEL_SCHEMA}.parent(id))")
    # Rows, spread over the parents, so that whether the check scans them all is
    # a question with an answer: pointing every row at one parent would make a
    # sequential scan genuinely cheaper and the plan comparison meaningless.
    await client.execute_sql(f"INSERT INTO {MODEL_SCHEMA}.fk_unindexed SELECT i, 1 + i %% 1000 FROM generate_series(1, 20000) i".replace("%%", "%"))
    await client.execute_sql(f"CREATE TABLE {MODEL_SCHEMA}.fk_indexed (id int PRIMARY KEY, parent_id bigint REFERENCES {MODEL_SCHEMA}.parent(id))")
    await client.execute_sql(f"CREATE INDEX ON {MODEL_SCHEMA}.fk_indexed (parent_id)")
    await client.execute_sql(
        f"CREATE TABLE {MODEL_SCHEMA}.fk_prefixed (id int PRIMARY KEY, parent_id bigint REFERENCES {MODEL_SCHEMA}.parent(id), at timestamptz)"
    )
    await client.execute_sql(f"CREATE INDEX ON {MODEL_SCHEMA}.fk_prefixed (parent_id, at)")
    await client.execute_sql(
        f"CREATE TABLE {MODEL_SCHEMA}.fk_reversed "
        f"(id int PRIMARY KEY, a bigint, c text, FOREIGN KEY (a, c) REFERENCES {MODEL_SCHEMA}.parent(id, code))"
    )
    await client.execute_sql(f"CREATE INDEX ON {MODEL_SCHEMA}.fk_reversed (c, a)")

    # foreign_key_type_mismatch: integer referencing bigint.
    await client.execute_sql(f"CREATE TABLE {MODEL_SCHEMA}.fk_mismatched (id int PRIMARY KEY, parent_id int REFERENCES {MODEL_SCHEMA}.parent(id))")
    await client.execute_sql(f"CREATE INDEX ON {MODEL_SCHEMA}.fk_mismatched (parent_id)")

    # missing_primary_key.
    await client.execute_sql(f"CREATE TABLE {MODEL_SCHEMA}.no_key (a int, b text)")

    # naive_timestamp and blank_padded_char.
    await client.execute_sql(f"CREATE TABLE {MODEL_SCHEMA}.naive (id int PRIMARY KEY, at timestamp, code char(10))")

    # column_order_padding: boolean between bigints, seven bytes a row each time.
    # Enough rows that the saving crosses the rule's minimum — below that it is
    # not worth a table rewrite and the rule is right to stay quiet.
    await client.execute_sql(f"CREATE TABLE {MODEL_SCHEMA}.unpacked (a boolean, b bigint, c boolean, d bigint, e boolean, f bigint)")
    await client.execute_sql(f"INSERT INTO {MODEL_SCHEMA}.unpacked SELECT true, i, false, i, true, i FROM generate_series(1, 100000) i")

    # nullable_never_null and low_cardinality_text.
    await client.execute_sql(
        f"""
        CREATE TABLE {MODEL_SCHEMA}.stats_driven (
            id int PRIMARY KEY,
            parent_id bigint REFERENCES {MODEL_SCHEMA}.parent(id),
            status text
        )
        """
    )
    await client.execute_sql(f"CREATE INDEX ON {MODEL_SCHEMA}.stats_driven (parent_id)")
    await client.execute_sql(
        f"""
        INSERT INTO {MODEL_SCHEMA}.stats_driven
        SELECT i, 1, (ARRAY['open', 'paid', 'void'])[1 + i %% 3] FROM generate_series(1, 20000) i
        """.replace("%%", "%")
    )

    # The clean model: keyed, indexed, packed, with types that mean what they say.
    await client.execute_sql(f"CREATE TABLE {CLEAN_SCHEMA}.parent (id bigint PRIMARY KEY, created_at timestamptz NOT NULL, name text NOT NULL)")
    await client.execute_sql(
        f"""
        CREATE TABLE {CLEAN_SCHEMA}.child (
            id bigint PRIMARY KEY,
            parent_id bigint NOT NULL REFERENCES {CLEAN_SCHEMA}.parent(id),
            created_at timestamptz NOT NULL,
            body text NOT NULL
        )
        """
    )
    await client.execute_sql(f"CREATE INDEX ON {CLEAN_SCHEMA}.child (parent_id)")
    await client.execute_sql(f"INSERT INTO {CLEAN_SCHEMA}.parent SELECT i, now(), 'name ' || i FROM generate_series(1, 20000) i")
    await client.execute_sql(f"INSERT INTO {CLEAN_SCHEMA}.child SELECT i, i, now(), 'body ' || i FROM generate_series(1, 20000) i")

    for schema in (MODEL_SCHEMA, CLEAN_SCHEMA):
        await client.execute_sql(f"ANALYZE {schema}.parent")
    await client.execute_sql(f"ANALYZE {MODEL_SCHEMA}.fk_unindexed")
    await client.execute_sql(f"ANALYZE {MODEL_SCHEMA}.unpacked")
    await client.execute_sql(f"ANALYZE {MODEL_SCHEMA}.stats_driven")
    await client.execute_sql(f"ANALYZE {CLEAN_SCHEMA}.child")

    try:
        yield client
    finally:
        for schema in (MODEL_SCHEMA, CLEAN_SCHEMA):
            await client.execute_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await client.close()


def tables_for(findings, rule):
    return {f.table for f in findings if f.rule.value == rule}


@pytest.mark.asyncio
async def test_a_clean_model_produces_no_findings_at_all(model_pg):
    """The one that matters. Every rule below fires next door; none may fire here."""
    findings = await model_pg.review_data_model(schema=CLEAN_SCHEMA, min_rows=100)

    assert findings == []


@pytest.mark.asyncio
async def test_system_schemas_are_never_reviewed(model_pg):
    assert await model_pg.review_data_model(schema="pg_catalog") == []
    assert await model_pg.review_data_model(schema="information_schema") == []


@pytest.mark.asyncio
async def test_an_unindexed_foreign_key_is_found_and_the_indexed_ones_are_not(model_pg):
    findings = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="unindexed_foreign_key")

    assert tables_for(findings, "unindexed_foreign_key") == {"fk_unindexed"}


@pytest.mark.asyncio
async def test_the_recommended_index_actually_removes_the_scan(model_pg):
    """The rule's whole claim is that checking this key scans the child table.

    A DELETE's own plan does not show it — the check runs inside a trigger — so
    this plans the query that trigger runs. Without the index it is a sequential
    scan of the child; with the index the rule recommends it must not be.
    """
    (finding,) = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="unindexed_foreign_key")
    ri_lookup = f"EXPLAIN SELECT 1 FROM {MODEL_SCHEMA}.fk_unindexed x WHERE parent_id OPERATOR(pg_catalog.=) 1::bigint FOR KEY SHARE OF x"

    before = " ".join(row["QUERY PLAN"] for row in await model_pg.execute_sql(ri_lookup))
    for statement in finding.remediation:
        # CONCURRENTLY cannot run inside a transaction block, and execute_sql
        # wraps each statement in one.
        await model_pg.execute_sql(statement.replace("CONCURRENTLY ", ""))
    await model_pg.execute_sql(f"ANALYZE {MODEL_SCHEMA}.fk_unindexed")
    after = " ".join(row["QUERY PLAN"] for row in await model_pg.execute_sql(ri_lookup))

    assert "Seq Scan" in before
    assert "Seq Scan" not in after
    assert "fk_unindexed_parent_id_idx" in after
    assert await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="unindexed_foreign_key") == []


@pytest.mark.asyncio
async def test_a_type_mismatch_is_found_and_the_matching_keys_are_not(model_pg):
    findings = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="foreign_key_type_mismatch")

    assert tables_for(findings, "foreign_key_type_mismatch") == {"fk_mismatched"}
    assert findings[0].remediation == [f"ALTER TABLE {MODEL_SCHEMA}.fk_mismatched ALTER COLUMN parent_id TYPE bigint;"]


@pytest.mark.asyncio
async def test_a_table_with_no_primary_key_is_found(model_pg):
    findings = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="missing_primary_key")

    assert tables_for(findings, "missing_primary_key") == {"no_key", "unpacked"}


@pytest.mark.asyncio
async def test_the_type_rules_find_their_columns_and_no_others(model_pg):
    findings = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="naive_timestamp,blank_padded_char")

    assert {(f.rule.value, f.table, f.columns[0]) for f in findings} == {
        ("naive_timestamp", "naive", "at"),
        ("blank_padded_char", "naive", "code"),
    }


@pytest.mark.asyncio
async def test_column_order_padding_predicts_the_real_saving(model_pg):
    """Run the CREATE TABLE it generates, copy the rows in, and compare the page
    counts against the prediction. This is the only way to know the rule is
    right, and it is how the estimate behind it was corrected in the first place.
    """
    (finding,) = [
        f for f in await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="column_order_padding", min_rows=100) if f.table == "unpacked"
    ]

    assert finding.confidence.value == "proven", "every column is fixed-width, so the saving is exact"

    create = next(statement for statement in finding.remediation if statement.startswith("CREATE TABLE"))
    await model_pg.execute_sql(create)
    packed_columns = ", ".join(f'"{column}"' for column in finding.columns)
    await model_pg.execute_sql(f"INSERT INTO {MODEL_SCHEMA}.unpacked_packed SELECT {packed_columns} FROM {MODEL_SCHEMA}.unpacked")
    # relpages is bookkeeping, not a live count: ANALYZE is what sets it.
    await model_pg.execute_sql(f"ANALYZE {MODEL_SCHEMA}.unpacked_packed")

    rows = await model_pg.execute_sql(
        f"SELECT relname, relpages FROM pg_class WHERE relname IN ('unpacked', 'unpacked_packed') AND relnamespace = '{MODEL_SCHEMA}'::regnamespace"
    )
    pages = {row["relname"]: row["relpages"] for row in rows}

    assert pages["unpacked"] == finding.evidence["current_pages"]
    assert pages["unpacked_packed"] == finding.evidence["packed_pages"]


@pytest.mark.asyncio
async def test_the_statistics_driven_rules_are_candidates_that_carry_their_proof(model_pg):
    findings = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="nullable_never_null,low_cardinality_text", min_rows=100)

    assert findings, "the fixture holds one of each"
    for finding in findings:
        assert finding.confidence.value == "candidate"
        assert finding.verify, "a candidate has to say how to settle it"
        # And the query it offers has to run.
        await model_pg.execute_sql(finding.verify.rstrip(";"))


@pytest.mark.asyncio
async def test_a_never_null_foreign_key_is_found_but_ordinary_columns_are_not(model_pg):
    """Restricted to key columns on purpose: most columns hold no NULLs, and
    reporting all of them would bury the review."""
    findings = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="nullable_never_null", min_rows=100)

    # Two foreign keys that are optional in the schema and populated in every
    # row, and one column of a unique index that NULLs could have slipped past.
    assert {(f.table, f.columns[0]) for f in findings} == {
        ("stats_driven", "parent_id"),
        ("fk_unindexed", "parent_id"),
        ("parent", "code"),
    }
    by_column = {f.columns[0]: f for f in findings}
    assert by_column["parent_id"].evidence["in_foreign_key"]
    assert by_column["code"].evidence["in_unique_index"]


@pytest.mark.asyncio
async def test_a_low_cardinality_column_reports_the_values_it_saw(model_pg):
    findings = await model_pg.review_data_model(schema=MODEL_SCHEMA, rules="low_cardinality_text", min_rows=100)

    (finding,) = findings
    assert finding.columns == ["status"]
    assert finding.evidence["sampled_values"] == ["open", "paid", "void"]


@pytest.mark.asyncio
async def test_proven_only_drops_the_inferred_findings(model_pg):
    everything = await model_pg.review_data_model(schema=MODEL_SCHEMA, min_rows=100)
    proven = await model_pg.review_data_model(schema=MODEL_SCHEMA, min_rows=100, proven_only=True)

    assert {f.confidence.value for f in proven} == {"proven"}
    assert len(proven) < len(everything)


@pytest.mark.asyncio
async def test_the_review_runs_in_restricted_mode(model_pg):
    """Every rule is a read, so the safe-SQL parser has to accept all of them."""
    async with PgDebug(DATABASE_URI, access_mode=AccessMode.RESTRICTED) as restricted:
        findings = await restricted.review_data_model(schema=MODEL_SCHEMA, min_rows=100)

    unrestricted = await model_pg.review_data_model(schema=MODEL_SCHEMA, min_rows=100)
    assert [f.summary for f in findings] == [f.summary for f in unrestricted]


def test_cli_model_command_against_a_real_database(capsys, monkeypatch, model_pg):
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")

    assert cli.main(["--json", "model", "--schema", CLEAN_SCHEMA]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert cli.main(["model", "--schema", MODEL_SCHEMA, "--exit-code"]) == cli.UNHEALTHY_EXIT_CODE


# -- Workload entries the advisor cannot use --------------------------------


@pytest.mark.asyncio
async def test_a_view_in_the_workload_does_not_abort_the_analysis(pg):
    """HypoPG raises on a view rather than declining it, so one ordinary query
    against one used to take the whole run with it."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    result = await pg.analyze_query_indexes([f"SELECT * FROM {SCHEMA}.paid_orders WHERE customer_id = 42"])

    assert "error" not in result
    # The index belongs on the table underneath, which this query does not name.
    assert result["recommendations"] == "No index recommendations found."


@pytest.mark.asyncio
async def test_a_view_alongside_a_table_still_yields_the_table_index(pg):
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    result = await pg.analyze_query_indexes(
        [f"SELECT o.* FROM {SCHEMA}.paid_orders p JOIN {SCHEMA}.orders o ON o.id = p.id WHERE o.customer_id = 42"]
    )

    assert "error" not in result
    assert any("orders" in rec["index_definition"] for rec in result["recommendations"])


@pytest.mark.asyncio
async def test_an_unusable_query_is_skipped_rather_than_ending_the_analysis(pg):
    """The reported failure: pg_stat_statements hands back text that is not SQL
    — '<insufficient privilege>' for another role's query — and the advisor
    stopped at the first one."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    advisor = DatabaseTuningAdvisor(await pg.sql_driver())
    workload = [
        {"query": HIDDEN_QUERY_TEXT, "queryid": 1, "calls": 100, "avg_exec_time": 10.0},
        {"query": f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 42", "queryid": 2, "calls": 100, "avg_exec_time": 10.0},
    ]

    session = await advisor.analyze_workload(workload=workload)

    assert session.error is None
    assert [rec.table for rec in session.recommendations] == [f"{SCHEMA}.orders"]


@pytest.mark.asyncio
async def test_a_wholly_unusable_workload_explains_itself(pg):
    """Otherwise this reports "no index recommendations", which reads as
    "your indexes are fine"."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    advisor = DatabaseTuningAdvisor(await pg.sql_driver())
    workload = [{"query": HIDDEN_QUERY_TEXT, "queryid": 1, "calls": 100, "avg_exec_time": 10.0}]

    session = await advisor.analyze_workload(workload=workload)

    assert session.recommendations == []
    assert "pg_read_all_stats" in (session.error or "")


@pytest.mark.asyncio
async def test_a_failed_statement_leaves_the_session_usable(pg):
    """A pinned connection is not returned to the pool between statements, so
    without a rollback one error would abort every statement after it."""
    driver = await pg.sql_driver()

    async with driver.session():
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            await driver.execute_query("SELECT 1 = 'not an integer'")

        rows = await driver.execute_query("SELECT 1 AS ok")

    assert rows is not None
    assert rows[0].cells["ok"] == 1


@pytest.mark.asyncio
async def test_hypothetical_indexes_survive_a_failed_statement(pg):
    """The rollback clears the aborted transaction; HypoPG keeps its indexes in
    backend memory, so the session's state is not collateral damage."""
    if not await has_extension(pg, "hypopg"):
        pytest.skip("hypopg is not installed")

    driver = await pg.sql_driver()

    async with driver.session():
        await driver.execute_query(f"SELECT hypopg_create_index('CREATE INDEX ON {SCHEMA}.orders (customer_id)')")
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            await driver.execute_query("SELECT 1 = 'not an integer'")

        remaining = await driver.execute_query("SELECT count(*) AS n FROM hypopg()")
        assert remaining is not None
        assert remaining[0].cells["n"] == 1


# -- Telling our own queries apart from the workload ------------------------


@pytest.mark.asyncio
async def test_our_own_queries_are_marked_in_pg_stat_statements(pg):
    """A leading comment survives into the text pg_stat_statements records,
    which is the whole basis for telling pg_debug's queries from the workload.
    """
    if not await has_extension(pg, "pg_stat_statements"):
        pytest.skip("pg_stat_statements is not installed")

    await pg.execute_sql("SELECT pg_stat_statements_reset()")
    await pg.analyze_db_health("table")

    rows = await pg.execute_sql("SELECT count(*) FILTER (WHERE query LIKE '/* pg\\_debug */%') AS tagged, count(*) AS total FROM pg_stat_statements")

    assert rows[0]["tagged"] > 0, "the health check's own queries should be marked"
    # Nothing should carry the marker twice, in either access mode.
    doubled = await pg.execute_sql("SELECT count(*) AS n FROM pg_stat_statements WHERE query LIKE '/* pg\\_debug */ /* pg\\_debug */%'")
    assert doubled[0]["n"] == 0


@pytest.mark.asyncio
async def test_restricted_mode_marks_queries_exactly_once(pg):
    if not await has_extension(pg, "pg_stat_statements"):
        pytest.skip("pg_stat_statements is not installed")

    await pg.execute_sql("SELECT pg_stat_statements_reset()")
    async with PgDebug(DATABASE_URI, access_mode=AccessMode.RESTRICTED) as restricted:
        await restricted.analyze_db_health("table")

    rows = await pg.execute_sql("SELECT count(*) AS n FROM pg_stat_statements WHERE query LIKE '/* pg\\_debug */ /* pg\\_debug */%'")

    assert rows[0]["n"] == 0


@pytest.mark.asyncio
async def test_top_queries_reports_the_workload_and_not_pg_debug(pg):
    """Without this the tool's own catalog reads take up slots in its own
    top-N, and inflate the denominator every resource fraction is measured
    against."""
    if not await has_extension(pg, "pg_stat_statements"):
        pytest.skip("pg_stat_statements is not installed")

    await pg.execute_sql("SELECT pg_stat_statements_reset()")

    # An application, on its own connection: anything run through the client is
    # marked as ours, which is the point — a query pg_debug ran to answer a
    # question about the workload is not part of that workload.
    async with await psycopg.AsyncConnection.connect(DATABASE_URI or "") as app:
        workload_query = f"SELECT count(*) FROM {SCHEMA}.orders WHERE status = 'paid'"
        for _ in range(3):
            await app.execute(workload_query)

    # ...and a good deal of pg_debug's own traffic on top of it.
    await pg.analyze_db_health("table")
    await pg.review_data_model(schema=SCHEMA)

    top = await pg.get_top_queries(sort_by="total_time", limit=20)
    queries = [row["query"] for row in top]

    assert any(f"{SCHEMA}.orders" in query for query in queries), "the workload query should be reported"
    assert not any("/* pg_debug */" in query for query in queries), "our own queries should not be"
    assert not any(query.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK")) for query in queries)


# -- Migration review -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_migration_review_measures_the_tables_it_touches(pg):
    reviews = await pg.check_migration(f"ALTER TABLE {SCHEMA}.orders ALTER COLUMN status TYPE varchar(20)")

    assert len(reviews) == 1
    assert reviews[0].table == f"{SCHEMA}.orders"
    assert reviews[0].table_bytes and reviews[0].table_bytes > 0, "the size is what decides whether this is a pause or an outage"


@pytest.mark.asyncio
async def test_volatility_is_resolved_against_the_catalog(pg):
    """now() is stable and does not rewrite; gen_random_uuid() is volatile and
    does. Only pg_proc can tell them apart, and the answer is checked here
    against the same server the claim is about."""
    stable = await pg.check_migration(f"ALTER TABLE {SCHEMA}.orders ADD COLUMN seen timestamptz DEFAULT now()")
    volatile = await pg.check_migration(f"ALTER TABLE {SCHEMA}.orders ADD COLUMN uid uuid DEFAULT gen_random_uuid()")

    assert stable[0].rewrites_table is False
    assert volatile[0].rewrites_table is True


@pytest.mark.asyncio
async def test_the_lock_levels_it_reports_are_the_ones_the_server_takes(pg):
    """The whole table of lock levels was measured rather than recalled. This
    re-measures three of them, so a future PostgreSQL that changes one is
    caught here rather than believed."""
    cases = [
        (f"ALTER TABLE {SCHEMA}.orders ADD CONSTRAINT chk_total CHECK (total >= 0)", "AccessExclusiveLock"),
        (f"ALTER TABLE {SCHEMA}.orders ADD CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES {SCHEMA}.customers(id)", "ShareRowExclusiveLock"),
        (f"CREATE INDEX idx_probe ON {SCHEMA}.orders (status)", "ShareLock"),
    ]
    expected = {
        "AccessExclusiveLock": "ACCESS EXCLUSIVE",
        "ShareRowExclusiveLock": "SHARE ROW EXCLUSIVE",
        "ShareLock": "SHARE",
    }

    held_locks = f"SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'relation' AND relation = '{SCHEMA}.orders'::regclass"

    async with await psycopg.AsyncConnection.connect(DATABASE_URI or "") as conn:
        for statement, strongest in cases:
            reported = (await pg.check_migration(statement))[0]
            await conn.execute(cast(LiteralString, statement))
            rows = await (await conn.execute(held_locks)).fetchall()
            await conn.rollback()

            held = {row[0] for row in rows}
            assert strongest in held, f"{statement} was expected to take {strongest}, took {held}"
            assert reported.lock.value == expected[strongest]


@pytest.mark.asyncio
async def test_the_safer_rewrite_it_suggests_actually_runs(pg):
    """Remediation nobody can paste is worse than none."""
    review = (await pg.check_migration(f"ALTER TABLE {SCHEMA}.orders ALTER COLUMN status SET NOT NULL"))[0]

    assert review.safer
    for statement in review.safer:
        await pg.execute_sql(statement.split("  --")[0])

    columns = await pg.execute_sql(f"SELECT attnotnull FROM pg_attribute WHERE attrelid = '{SCHEMA}.orders'::regclass AND attname = 'status'")
    assert columns[0]["attnotnull"] is True


def test_cli_check_migration_against_a_real_database(capsys, monkeypatch, pg):
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")

    assert cli.main(["--json", "check-migration", f"CREATE INDEX i ON {SCHEMA}.orders (status)"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["kind"] == "create_index"
    assert payload[0]["table_bytes"] > 0


# -- Autovacuum -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_table_with_autovacuum_disabled_is_reported_as_that(pg):
    """Not as "behind": the fix for a switched-off table is not the fix for a
    late one, so the two must not read the same."""
    await pg.execute_sql(f"CREATE TABLE {SCHEMA}.frozen (id int, pad text) WITH (autovacuum_enabled = false)")
    await pg.execute_sql(f"INSERT INTO {SCHEMA}.frozen SELECT i, 'x' FROM generate_series(1, 40000) i")
    await pg.execute_sql(f"DELETE FROM {SCHEMA}.frozen WHERE id % 2 = 0")
    await pg.execute_sql(f"ANALYZE {SCHEMA}.frozen")

    calc = AutovacuumCalc(await pg.sql_driver())
    check = await calc.autovacuum_backlog_check()

    flagged = [f for f in check.findings if f["table"] == "frozen"]
    assert flagged, "a table with 20,000 dead rows and autovacuum off should be reported"
    assert flagged[0]["reason"] == "disabled"
    assert flagged[0]["autovacuum_enabled"] is False


@pytest.mark.asyncio
async def test_a_read_only_idle_transaction_does_not_hold_the_horizon(pg):
    """The advice to hunt "idle in transaction" is too broad. A READ COMMITTED
    transaction that has only read holds neither a snapshot nor a transaction
    id between statements, and blocks no cleanup at all."""
    calc = AutovacuumCalc(await pg.sql_driver(), min_transaction_seconds=0)

    async with await psycopg.AsyncConnection.connect(DATABASE_URI or "") as idle:
        await idle.execute(f"SELECT count(*) FROM {SCHEMA}.orders")
        check = await calc.vacuum_horizon_check()
        await idle.rollback()

    assert check.healthy, f"nothing should be reported, got {check.summary}"


@pytest.mark.asyncio
async def test_a_transaction_holding_a_snapshot_is_reported(pg):
    calc = AutovacuumCalc(await pg.sql_driver(), min_transaction_seconds=0)

    async with await psycopg.AsyncConnection.connect(DATABASE_URI or "") as holder:
        await holder.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        await holder.execute(f"SELECT count(*) FROM {SCHEMA}.orders")
        check = await calc.vacuum_horizon_check()
        await holder.rollback()

    assert not check.healthy
    assert any(f["kind"] == "transaction" and f["holds"] == "a snapshot" for f in check.findings)


@pytest.mark.asyncio
async def test_a_write_transaction_holds_a_transaction_id(pg):
    calc = AutovacuumCalc(await pg.sql_driver(), min_transaction_seconds=0)

    async with await psycopg.AsyncConnection.connect(DATABASE_URI or "") as holder:
        await holder.execute(f"INSERT INTO {SCHEMA}.customers (email) VALUES ('holder@example.com')")
        check = await calc.vacuum_horizon_check()
        await holder.rollback()

    assert not check.healthy
    assert any(f["holds"] == "a transaction id" for f in check.findings)


# -- Configuration review ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_configuration_review_argues_from_this_server_s_counters(pg):
    findings = await pg.review_configuration()

    by_setting = {finding.setting: finding for finding in findings}
    # Whatever it reports, every finding has to carry the measurement it rests on.
    for finding in findings:
        assert finding.evidence, f"{finding.setting} was reported with no evidence"

    # A default this server is not suffering from must not be reported.
    assert "maintenance_work_mem" not in by_setting, "nothing measures this, so nothing should claim it is wrong"


@pytest.mark.asyncio
async def test_a_healthy_cache_is_not_told_to_grow(pg):
    """shared_buffers below the database size is only a problem when reads are
    actually missing, and on this server they are not."""
    hit_rate = await pg.execute_sql(
        "SELECT blks_hit::float8 / nullif(blks_hit + blks_read, 0) AS rate FROM pg_stat_database WHERE datname = current_database()"
    )
    if (hit_rate[0]["rate"] or 0) < 0.99:
        pytest.skip("this server's cache is genuinely missing, so the finding is correct")

    findings = await pg.review_configuration()

    assert not any(finding.setting == "shared_buffers" for finding in findings)


@pytest.mark.asyncio
async def test_the_configuration_review_runs_in_restricted_mode(pg):
    async with PgDebug(DATABASE_URI, access_mode=AccessMode.RESTRICTED) as restricted:
        findings = await restricted.review_configuration()

    assert [f.setting for f in findings] == [f.setting for f in await pg.review_configuration()]


def test_cli_config_against_a_real_database(capsys, monkeypatch, pg):
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")

    assert cli.main(["--json", "config"]) == 0

    payload = json.loads(capsys.readouterr().out)
    for finding in payload:
        assert finding["evidence"], f"{finding['setting']} reported with no evidence"


# -- Partition advisor ------------------------------------------------------

PARTITION_SCHEMA = "pgdebug_partition_test"


@pytest_asyncio.fixture
async def partition_pg():
    """Two tables the same size, told apart only by what the queries do.

    ``events`` is filtered by time; ``ledger`` is not. Nothing about their
    shape distinguishes them — same row count, same timestamp column, same
    primary key — so if the advisor reaches different verdicts it can only be
    from the workload, which is the claim being tested.
    """
    client = PgDebug(DATABASE_URI)
    await client.execute_sql(f"DROP SCHEMA IF EXISTS {PARTITION_SCHEMA} CASCADE")
    await client.execute_sql(f"CREATE SCHEMA {PARTITION_SCHEMA}")

    for table in ("events", "ledger"):
        await client.execute_sql(
            f"""
            CREATE TABLE {PARTITION_SCHEMA}.{table} (
                id bigserial PRIMARY KEY,
                tenant_id int NOT NULL,
                created_at timestamptz NOT NULL,
                amount numeric
            )
            """
        )
        await client.execute_sql(
            f"INSERT INTO {PARTITION_SCHEMA}.{table} (tenant_id, created_at, amount) "
            f"SELECT (i % 7) + 1, now() - (i || ' minutes')::interval, i / 100.0 FROM generate_series(1, 20000) i"
        )
        await client.execute_sql(f"ANALYZE {PARTITION_SCHEMA}.{table}")

    try:
        yield client
    finally:
        await client.execute_sql(f"DROP SCHEMA IF EXISTS {PARTITION_SCHEMA} CASCADE")
        await client.close()


def workload_for(table, pruning_share):
    """Queries against ``table`` of which ``pruning_share`` restrict on the key.

    The weight is one per query when the caller supplies the list, so the share
    is just how many of them filter.
    """
    total = 10
    pruning = round(total * pruning_share)
    return [f"SELECT count(*) FROM {PARTITION_SCHEMA}.{table} WHERE created_at > now() - interval '{i} day'" for i in range(1, pruning + 1)] + [
        f"SELECT sum(amount) FROM {PARTITION_SCHEMA}.{table} WHERE amount > {i}" for i in range(total - pruning)
    ]


@pytest.mark.asyncio
async def test_a_table_the_workload_filters_by_time_is_recommended(partition_pg):
    candidates = await partition_pg.suggest_partitions(sql=workload_for("events", 0.9), min_table_bytes=0)

    events = next(c for c in candidates if c.table == "events")
    assert events.recommended is True
    assert events.column == "created_at"
    assert events.strategy is Strategy.RANGE


@pytest.mark.asyncio
async def test_an_identical_table_the_workload_does_not_filter_is_refused(partition_pg):
    """Same shape, same size, same timestamp. Only the queries differ, and the
    verdict has to follow them rather than the table."""
    candidates = await partition_pg.suggest_partitions(sql=workload_for("ledger", 0.2), min_table_bytes=0)

    ledger = next(c for c in candidates if c.table == "ledger")
    assert ledger.recommended is False
    assert ledger.pruning_share < 0.5
    assert ledger.ddl == []


@pytest.mark.asyncio
async def test_the_primary_key_is_named_as_the_thing_that_has_to_change(partition_pg):
    candidates = await partition_pg.suggest_partitions(sql=workload_for("events", 1.0), min_table_bytes=0)

    events = next(c for c in candidates if c.table == "events")
    assert any("events_pkey" in blocker for blocker in events.blockers)


@pytest.mark.asyncio
async def test_the_generated_ddl_actually_runs(partition_pg):
    """The claim that costs the most if it is wrong. A sketch that fails on the
    blocker the advisor itself named would be found by whoever tried it, after
    they had already decided to do this."""
    candidates = await partition_pg.suggest_partitions(sql=workload_for("events", 1.0), min_table_bytes=0)
    events = next(c for c in candidates if c.table == "events")

    for statement in events.ddl:
        if statement.startswith("--"):
            continue
        await partition_pg.execute_sql(statement)

    partitions = await partition_pg.execute_sql(
        f"SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = '{PARTITION_SCHEMA}' AND c.relispartition"
    )
    assert partitions[0]["n"] > 0


@pytest.mark.asyncio
async def test_the_partitioned_table_the_ddl_builds_actually_prunes(partition_pg):
    """The whole recommendation rests on this. If the plan still reads every
    partition, the advice was wrong however good the reasoning looked."""
    candidates = await partition_pg.suggest_partitions(sql=workload_for("events", 1.0), min_table_bytes=0)
    events = next(c for c in candidates if c.table == "events")

    for statement in events.ddl:
        if not statement.startswith("--"):
            await partition_pg.execute_sql(statement)
    await partition_pg.execute_sql(
        f"INSERT INTO {PARTITION_SCHEMA}.events_partitioned (tenant_id, created_at, amount) "
        f"SELECT tenant_id, created_at, amount FROM {PARTITION_SCHEMA}.events"
    )
    await partition_pg.execute_sql(f"ANALYZE {PARTITION_SCHEMA}.events_partitioned")

    plan = await partition_pg.explain_query(f"SELECT count(*) FROM {PARTITION_SCHEMA}.events_partitioned WHERE created_at > now() - interval '1 day'")

    assert "Subplans Removed" in plan.value, "the partition key does not prune, so the recommendation was wrong"


@pytest.mark.asyncio
async def test_a_table_no_query_touches_is_not_judged(partition_pg):
    candidates = await partition_pg.suggest_partitions(sql=["SELECT 1"], min_table_bytes=0)

    assert [c.table for c in candidates if c.schema == PARTITION_SCHEMA] == []


@pytest.mark.asyncio
async def test_the_advisor_runs_in_restricted_mode(partition_pg):
    queries = workload_for("events", 0.9)
    async with PgDebug(DATABASE_URI, access_mode=AccessMode.RESTRICTED) as restricted:
        candidates = await restricted.suggest_partitions(sql=queries, min_table_bytes=0)

    unrestricted = await partition_pg.suggest_partitions(sql=queries, min_table_bytes=0)
    assert [(c.qualified_table, c.recommended) for c in candidates] == [(c.qualified_table, c.recommended) for c in unrestricted]


def test_cli_partitions_against_a_real_database(capsys, monkeypatch, partition_pg):
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")

    assert cli.main(["--json", "partitions", "--min-size", "1MB"]) == 0

    for candidate in json.loads(capsys.readouterr().out):
        assert candidate["evidence"], f"{candidate['table']} reported with no evidence"
        assert candidate["reasons"], f"{candidate['table']} reported with no reasoning"
