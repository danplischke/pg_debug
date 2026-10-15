"""The data model lints against a real server, each on a fixture schema built to trip it."""

from typing import LiteralString
from typing import cast

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.model import Confidence
from pg_debug.model import RuleId
from pg_debug.model import Severity


def q(statement: str) -> LiteralString:
    """A statement built from this module's own fixtures, as psycopg's types want it."""
    return cast(LiteralString, statement)


FIXTURE = [
    "CREATE SCHEMA lint",
    "CREATE TABLE lint.docs (id bigint PRIMARY KEY, body json, ok jsonb, created timestamp, created_tz timestamptz)",
    "CREATE TABLE lint.parent (id bigint PRIMARY KEY)",
    "CREATE TABLE lint.child (id bigint PRIMARY KEY, parent_id integer REFERENCES lint.parent (id))",
    "CREATE TABLE lint.counter (id serial PRIMARY KEY, note text)",
    "SELECT setval('lint.counter_id_seq', 1800000000)",
    "CREATE TABLE lint.refs (id bigint PRIMARY KEY, counter_id integer REFERENCES lint.counter (id))",
    "CREATE TABLE lint.ident (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY)",
    "ALTER TABLE lint.ident ALTER COLUMN id RESTART WITH 1200000000",
    "INSERT INTO lint.ident DEFAULT VALUES",
    "CREATE TABLE lint.young (id serial PRIMARY KEY)",
    "INSERT INTO lint.young DEFAULT VALUES",
    "CREATE TABLE lint.sparse (id bigint PRIMARY KEY, maybe int, dense int)",
    "INSERT INTO lint.sparse SELECT g, CASE WHEN g % 10 = 0 THEN g END, g FROM generate_series(1, 5000) g",
    "CREATE INDEX sparse_maybe_idx ON lint.sparse (maybe)",
    "CREATE INDEX sparse_dense_idx ON lint.sparse (dense)",
    "CREATE INDEX sparse_maybe_partial_idx ON lint.sparse (maybe) WHERE maybe IS NOT NULL",
    "ANALYZE lint.sparse",
]


@pytest.fixture(scope="class")
def url(test_postgres_connection_string) -> str:
    url = test_postgres_connection_string[0]
    with psycopg.connect(url, autocommit=True) as conn:
        for statement in FIXTURE:
            conn.execute(q(statement))
    return url


def by_rule(findings, rule):
    return {f"{f.qualified_table}.{','.join(f.columns)}": f for f in findings if f.rule is rule}


class TestModelLints:
    @pytest.mark.asyncio
    async def test_json_is_a_candidate_and_jsonb_is_left_alone(self, url):
        async with PgDebug(url, access_mode="restricted") as pg:
            findings = await pg.review_data_model(schema="lint", rules="json_column")

        found = by_rule(findings, RuleId.JSON_COLUMN)
        assert set(found) == {"lint.docs.body"}
        assert found["lint.docs.body"].confidence is Confidence.CANDIDATE

    @pytest.mark.asyncio
    async def test_the_verify_query_for_json_runs_and_counts_duplicate_keys(self, url):
        async with PgDebug(url) as pg:
            [finding] = await pg.review_data_model(schema="lint", rules="json_column")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("""INSERT INTO lint.docs (id, body) VALUES (1, '{"a": 1, "a": 2}'), (2, '[1, 2]'), (3, '{"b": 1}')""")
            row = conn.execute(q(finding.verify or "")).fetchone()
            conn.execute("DELETE FROM lint.docs")

        assert row is not None and row[0] == 1

    @pytest.mark.asyncio
    async def test_naive_timestamps_and_mismatched_foreign_keys_are_found(self, url):
        async with PgDebug(url, access_mode="restricted") as pg:
            findings = await pg.review_data_model(schema="lint", rules="naive_timestamp,foreign_key_type_mismatch")

        assert set(by_rule(findings, RuleId.NAIVE_TIMESTAMP)) == {"lint.docs.created"}
        assert set(by_rule(findings, RuleId.FOREIGN_KEY_TYPE_MISMATCH)) == {"lint.child.parent_id"}

    @pytest.mark.asyncio
    async def test_serial_and_identity_keys_near_overflow_are_found_and_a_young_one_is_not(self, url):
        async with PgDebug(url, access_mode="restricted") as pg:
            findings = await pg.review_data_model(schema="lint", rules="primary_key_overflow")

        found = by_rule(findings, RuleId.PRIMARY_KEY_OVERFLOW)
        assert set(found) == {"lint.counter.id", "lint.ident.id"}
        assert found["lint.counter.id"].severity is Severity.HIGH
        assert found["lint.counter.id"].remediation == [
            "ALTER TABLE lint.counter ALTER COLUMN id TYPE bigint;",
            "ALTER SEQUENCE lint.counter_id_seq AS bigint;",
            "ALTER TABLE lint.refs ALTER COLUMN counter_id TYPE bigint;",
        ]
        assert found["lint.ident.id"].severity is Severity.MEDIUM
        assert found["lint.ident.id"].remediation == ["ALTER TABLE lint.ident ALTER COLUMN id TYPE bigint;"]

    @pytest.mark.asyncio
    async def test_the_remediation_for_an_identity_key_leaves_nothing_at_the_old_limit(self, url):
        """The claim that the identity's sequence follows its column, checked on the server."""
        async with PgDebug(url) as pg:
            findings = await pg.review_data_model(schema="lint", rules="primary_key_overflow")
        identity = by_rule(findings, RuleId.PRIMARY_KEY_OVERFLOW)["lint.ident.id"]
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("BEGIN")
            for statement in identity.remediation:
                conn.execute(q(statement))
            row = conn.execute("SELECT data_type FROM pg_sequences WHERE sequencename = 'ident_id_seq'").fetchone()
            conn.execute("ROLLBACK")

        assert row is not None and row[0] == "bigint"

    @pytest.mark.asyncio
    async def test_a_mostly_null_index_is_a_candidate_and_dense_or_partial_ones_are_not(self, url):
        async with PgDebug(url, access_mode="restricted") as pg:
            findings = await pg.review_data_model(schema="lint", rules="mostly_null_index", min_rows=1000)

        found = [f for f in findings if f.rule is RuleId.MOSTLY_NULL_INDEX]
        assert [f.evidence["index"] for f in found] == ["sparse_maybe_idx"]
        assert found[0].confidence is Confidence.CANDIDATE
        assert found[0].remediation[0] == (
            "CREATE INDEX CONCURRENTLY sparse_maybe_idx_not_null ON lint.sparse USING btree (maybe) WHERE maybe IS NOT NULL;"
        )
