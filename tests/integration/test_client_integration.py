"""End-to-end tests for the client and CLI against a real PostgreSQL server.

These are skipped unless PG_DEBUG_TEST_DATABASE_URI points at a database the
test may create and drop tables in. Unlike the other integration tests they do
not need Docker, so they can run against any server you already have:

    PG_DEBUG_TEST_DATABASE_URI=postgresql://postgres@localhost:5432/postgres \\
        uv run pytest tests/integration/test_client_integration.py
"""

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


def test_cli_against_a_real_database(capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URI", DATABASE_URI or "")

    assert cli.main(["--json", "execute", "SELECT 1 AS ok"]) == 0

    assert json.loads(capsys.readouterr().out) == [{"ok": 1}]


def test_cli_reports_connection_failures(capsys):
    assert cli.main(["-d", "postgresql://nobody:secret@127.0.0.1:1/nope", "schemas"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret" not in captured.err
