"""Partitioned tables that exist, against a real server: each finding from a table built to have it.

The check runs in restricted mode, as a monitoring role would run it, so every
statement it makes is shown to pass the allowlist and run read-only.
"""

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.health.checks.partition import PARTITION_HEALTH
from pg_debug.partition.recipes import attach_partition_recipe
from pg_debug.plans.result import PlanIssue
from pg_debug.severity import Severity

SETUP = """
-- A month-by-month table whose last partition ends within the month, with rows
-- already in its DEFAULT partition for a later month.
CREATE TABLE pgd_events (id bigint, ts timestamptz NOT NULL, v int) PARTITION BY RANGE (ts);
DO $$
DECLARE m timestamptz;
BEGIN
    FOR m IN SELECT generate_series(date_trunc('month', now()) - interval '2 months', date_trunc('month', now()), interval '1 month') LOOP
        EXECUTE format('CREATE TABLE pgd_events_%s PARTITION OF pgd_events FOR VALUES FROM (%L) TO (%L)',
                       to_char(m, 'YYYY_MM'), m, m + interval '1 month');
    END LOOP;
END $$;
CREATE TABLE pgd_events_default PARTITION OF pgd_events DEFAULT;
INSERT INTO pgd_events SELECT g, date_trunc('month', now()) + interval '3 months' + g * interval '1 minute', g FROM generate_series(1, 300) g;
INSERT INTO pgd_events SELECT g, date_trunc('month', now()) - interval '40 days' + (g % 60) * interval '1 day', g FROM generate_series(1, 6000) g;

-- Twenty hash partitions with two indexes each: 63 relations for a query that prunes nothing.
CREATE TABLE pgd_hashed (id bigint, k int NOT NULL, v int) PARTITION BY HASH (k);
CREATE INDEX ON pgd_hashed (id);
CREATE INDEX ON pgd_hashed (v);
DO $$
BEGIN
    FOR r IN 0..19 LOOP
        EXECUTE format('CREATE TABLE pgd_hashed_%s PARTITION OF pgd_hashed FOR VALUES WITH (MODULUS 20, REMAINDER %s)', r, r);
    END LOOP;
END $$;

-- A unique index on each partition and none on the parent.
CREATE TABLE pgd_users (id bigint, email text, d date NOT NULL) PARTITION BY RANGE (d);
CREATE TABLE pgd_users_a PARTITION OF pgd_users FOR VALUES FROM ('2000-01-01') TO ('3000-01-01');
CREATE TABLE pgd_users_b PARTITION OF pgd_users FOR VALUES FROM ('3000-01-01') TO ('4000-01-01');
CREATE UNIQUE INDEX ON pgd_users_a (email);
CREATE UNIQUE INDEX ON pgd_users_b (email);
ANALYZE;
"""


@pytest.fixture(scope="class")
def url(test_postgres_connection_string):
    url = test_postgres_connection_string[0]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SETUP)  # type: ignore[arg-type]
    yield url
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS pgd_events, pgd_hashed, pgd_users, pgd_events_next CASCADE")


async def _check(url: str):
    async with PgDebug(url, access_mode="restricted") as pg:
        return (await pg.analyze_db_health("partition"))[PARTITION_HEALTH]


class TestPartitionHealth:
    @pytest.mark.asyncio
    async def test_each_built_problem_is_found_in_restricted_mode(self, url):
        result = await _check(url)

        assert result.error is None, result.error
        assert not result.healthy
        by_kind = {}
        for finding in result.findings:
            by_kind.setdefault(finding["kind"], []).append(finding)

        (gap,) = by_kind["premade_partitions"]
        assert gap["table"] == "pgd_events" and gap["severity"] == "medium"
        assert any("ATTACH PARTITION" in step for step in gap["recipe"])

        (default,) = by_kind["default_partition_rows"]
        assert default["evidence"]["default_partition"] == "public.pgd_events_default"
        assert default["evidence"]["estimated_rows"] == 300

        (unique,) = by_kind["unique_per_partition"]
        assert unique["table"] == "pgd_users" and unique["column"] == "email"
        assert unique["evidence"]["partitions_with_index"] == 2

        (locks,) = by_kind["lock_pressure"]
        assert locks["table"] == "pgd_hashed"
        assert locks["evidence"]["unpruned_locks"] == 63, "measured: 21 tables and 42 indexes"
        assert locks["evidence"]["fast_path_slots"] == 16
        assert locks["confidence"] == "candidate"

        assert "pruning_disabled" not in by_kind

    @pytest.mark.asyncio
    async def test_the_unpruned_lock_count_is_what_a_query_takes(self, url):
        """The estimate is the count pg_locks shows for a query on every partition."""
        with psycopg.connect(url) as conn:
            conn.execute("SELECT count(*) FROM pgd_hashed WHERE v = 1")
            (held,) = conn.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' AND c.relname LIKE 'pgd_hashed%'"
            ).fetchone()  # type: ignore[misc]
            conn.rollback()
        result = await _check(url)
        (locks,) = [f for f in result.findings if f["kind"] == "lock_pressure"]
        assert locks["evidence"]["unpruned_locks"] == held

    @pytest.mark.asyncio
    async def test_pruning_switched_off_for_the_database_is_found(self, url):
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET enable_partition_pruning = off', current_database()); END $$")
        try:
            result = await _check(url)
        finally:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute("DO $$ BEGIN EXECUTE format('ALTER DATABASE %I RESET enable_partition_pruning', current_database()); END $$")

        (finding,) = [f for f in result.findings if f["kind"] == "pruning_disabled"]
        assert finding["severity"] == "high"
        assert finding["datname"]

    @pytest.mark.asyncio
    async def test_the_attach_recipe_passes_review_and_runs(self, url):
        """Reviewed against the database it is low throughout; run, it attaches the partition."""
        recipe = attach_partition_recipe(
            "public.pgd_events", "public.pgd_events_next", "ts", "'2100-01-01'", "'2100-02-01'", default="public.pgd_events_default"
        )
        async with PgDebug(url) as pg:
            reviews = await pg.check_migration("\n".join(recipe))
        assert {review.severity for review in reviews} == {Severity.LOW}, [(r.kind, r.severity) for r in reviews]

        plain = "CREATE TABLE public.pgd_events_x PARTITION OF public.pgd_events FOR VALUES FROM ('2101-01-01') TO ('2101-02-01')"
        async with PgDebug(url) as pg:
            (review,) = await pg.check_migration(plain)
        assert review.kind == "create_partition_of_scans_default", "the DEFAULT holds rows"

        with psycopg.connect(url, autocommit=True) as conn:
            for statement in recipe:
                if not statement.startswith("--"):
                    conn.execute(statement)  # type: ignore[arg-type]
            (attached,) = conn.execute(
                "SELECT count(*) FROM pg_inherits WHERE inhrelid = 'public.pgd_events_next'::regclass AND inhparent = 'public.pgd_events'::regclass"
            ).fetchone()  # type: ignore[misc]
        assert attached == 1

    @pytest.mark.asyncio
    async def test_a_condition_pruning_cannot_read_is_a_plan_finding_and_one_it_can_is_not(self, url):
        async with PgDebug(url) as pg:
            wrapped = await pg.diagnose_query("SELECT count(*) FROM pgd_events WHERE date(ts AT TIME ZONE 'UTC') = '1999-03-05'")
            direct = await pg.diagnose_query("SELECT count(*) FROM pgd_events WHERE ts >= '1999-03-05' AND ts < '1999-03-06'")

        (finding,) = [f for f in wrapped if f.issue is PlanIssue.UNPRUNED_PARTITIONS]
        assert finding.relation == "public.pgd_events"
        assert finding.evidence["partition_key"] == ["ts"]
        assert not [f for f in direct if f.issue is PlanIssue.UNPRUNED_PARTITIONS]
