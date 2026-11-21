"""compare-queries and top-queries --save/--compare against a real server."""

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.errors import InvalidArgumentError
from pg_debug.top_queries import QuerySnapshot

SETUP = """
DROP TABLE IF EXISTS pgd_skewed;
CREATE TABLE pgd_skewed AS
    SELECT g AS id, CASE WHEN g <= 90000 THEN 1 ELSE g END AS k FROM generate_series(1, 100000) g;
CREATE INDEX ON pgd_skewed (k);
ANALYZE pgd_skewed;
"""


@pytest.fixture(scope="class")
def url(test_postgres_connection_string):
    url = test_postgres_connection_string[0]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SETUP)  # type: ignore[arg-type]
    yield url
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS pgd_skewed")


class TestCompareQueries:
    @pytest.mark.asyncio
    async def test_a_skewed_value_gets_its_own_plan(self, url):
        async with PgDebug(url) as pg:
            comparison = await pg.compare_queries(["SELECT count(*) FROM pgd_skewed WHERE k = $1"], values=[[1], [95000]], repeat=2)

        common, rare = comparison.runs
        assert (common.label, rare.label) == ("A (1)", "A (95000)")
        assert common.rows == rare.rows == 1
        assert all(run.error is None and len(run.runs_ms) == 2 for run in comparison.runs)
        assert any("Index" in line for line in rare.shape), rare.shape
        assert common.fingerprint != rare.fingerprint
        assert any("different plans" in note for note in comparison.notes)

    @pytest.mark.asyncio
    async def test_restricted_mode_refuses_to_run_them(self, url):
        async with PgDebug(url, access_mode="restricted") as pg:
            with pytest.raises(InvalidArgumentError, match="restricted mode"):
                await pg.compare_queries(["SELECT 1"], repeat=1)


class TestTopQueriesSnapshot:
    @pytest.mark.asyncio
    async def test_what_ran_since_a_saved_snapshot_is_reported(self, url, tmp_path):
        with psycopg.connect(url, autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'").fetchone():
                pytest.skip("pg_stat_statements is not installed")
            conn.execute("SELECT count(*) FROM pgd_skewed WHERE id < 10 /*controller='snap'*/")
        path = tmp_path / "snapshot.json"
        async with PgDebug(url) as pg:
            (await pg.snapshot_top_queries()).save(path)
        with psycopg.connect(url, autocommit=True) as conn:
            for _ in range(3):
                conn.execute("SELECT count(*) FROM pgd_skewed WHERE id < 10 /*controller='snap'*/")
        async with PgDebug(url) as pg:
            sample = await pg.compare_top_queries(path, sort_by="total_time", limit=50)

        [ours] = [q for q in sample.queries if "pgd_skewed WHERE id <" in q["query"]]
        assert ours["calls"] == 3
        assert ours["tags"] == {"controller": "snap"}
        assert sample.since == QuerySnapshot.load(path).taken_at.isoformat()
