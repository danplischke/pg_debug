"""The build gate against a real server: definitions fingerprinted, plan shapes read, --static on a fresh schema."""

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.baseline import Baseline
from pg_debug.baseline import keys_of
from pg_debug.baseline import partition
from pg_debug.health.checks.index import DUPLICATE_INDEXES

TABLE = "pgdebug_gate_t"


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


@pytest.fixture
def schema(url):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(f"CREATE TABLE {TABLE} (id int PRIMARY KEY, a int, b int, at timestamp)")
        conn.execute(f"INSERT INTO {TABLE} SELECT g, g % 1000, g % 7, now() FROM generate_series(1, 20000) g")
        conn.execute(f"CREATE INDEX {TABLE}_a ON {TABLE} (a)")
        conn.execute(f"CREATE INDEX {TABLE}_ab ON {TABLE} (a, b)")
        conn.execute(f"ANALYZE {TABLE}")
    try:
        yield
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")


def execute(url, statement):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(statement)


@pytest.mark.asyncio
@pytest.mark.usefixtures("schema")
@pytest.mark.parametrize("access_mode", ["unrestricted", "restricted"])
async def test_a_redefined_duplicate_index_is_changed_not_known(url, access_mode):
    async with PgDebug(url, access_mode=access_mode) as pg:
        results = await pg.analyze_db_health("index", static=True)
        keys = keys_of("health", results)
        before = await pg.definition_fingerprints("health", keys)
    assert set(results) == {"Invalid index check", "Duplicate index check"}, "--static runs the catalog checks only"
    mine = [i for i, key in enumerate(keys) if key.get("table") == TABLE]
    assert [keys[i]["check"] for i in mine] == [DUPLICATE_INDEXES]
    accepted = Baseline("health", [{**key, "fingerprint": fp} for key, fp in zip(keys, before, strict=True) if fp is not None])

    # Still covered by an index of the same name, so the same finding by name;
    # but the covering index is another index now, not the one accepted.
    execute(url, f"DROP INDEX {TABLE}_ab")
    execute(url, f"CREATE INDEX {TABLE}_ab ON {TABLE} (a, at)")

    async with PgDebug(url, access_mode=access_mode) as pg:
        results = await pg.analyze_db_health("index", static=True)
        keys = keys_of("health", results)
        after = await pg.definition_fingerprints("health", keys)
    now = [{**key, "fingerprint": fp} if fp is not None else key for key, fp in zip(keys, after, strict=True)]

    split = partition("health", results, accepted, keys=now)
    changed = [key for key in split.changed if key.get("table") == TABLE]
    assert [key["index"] for key in changed] == [f"{TABLE}_a"]
    assert split.new_failures >= 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("schema")
@pytest.mark.parametrize("access_mode", ["unrestricted", "restricted"])
async def test_a_model_finding_on_a_column_whose_type_changed_is_changed(url, access_mode):
    async with PgDebug(url, access_mode=access_mode) as pg:
        findings = await pg.review_data_model(rules="naive_timestamp", static=True)
        keys = [key for key in keys_of("model", findings) if key["table"] == TABLE]
        [before] = await pg.definition_fingerprints("model", keys)
    assert before is not None

    execute(url, f"ALTER TABLE {TABLE} ALTER COLUMN at SET NOT NULL")

    async with PgDebug(url, access_mode=access_mode) as pg:
        [after] = await pg.definition_fingerprints("model", keys)
    assert after is not None and after != before


@pytest.mark.asyncio
@pytest.mark.usefixtures("schema")
@pytest.mark.parametrize("access_mode", ["unrestricted", "restricted"])
async def test_dropping_the_index_a_query_uses_changes_its_plan_shape(url, access_mode):
    queries = [f"-- name: by_a\nSELECT * FROM {TABLE} WHERE a = 5 AND b = 1;\nSELECT count(*) FROM {TABLE} WHERE id = $1"]
    async with PgDebug(url, access_mode=access_mode) as pg:
        before = await pg.plan_shapes(queries)
    assert [shape.error for shape in before] == [None, None]
    assert before[0].name == "by_a"
    assert any(f"using {TABLE}_ab" in line for line in before[0].shape), before[0].shape

    execute(url, f"DROP INDEX {TABLE}_ab")

    async with PgDebug(url, access_mode=access_mode) as pg:
        after = await pg.plan_shapes(queries)
    assert after[0].fingerprint != before[0].fingerprint
    assert after[1].fingerprint == before[1].fingerprint, "the other query's plan did not change"

    split = partition("plans", after, Baseline("plans", [shape.key() for shape in before]))
    assert [key["query"] for key in split.changed] == ["by_a"]
    assert split.new_failures == 1
