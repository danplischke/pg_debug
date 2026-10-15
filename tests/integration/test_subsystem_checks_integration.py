"""Slot retention, multixact age, collation versions and checksum failures, read from a real server.

Slots and databases belong to the server, not the test's database, so each
is named uniquely and dropped in ``finally``: an inactive slot left behind
retains WAL on whatever server the tests ran against.
"""

import uuid
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import psycopg
import pytest

from pg_debug import PgDebug
from pg_debug.baseline import health_keys
from pg_debug.health.checks.checksum import CHECKSUM_HEALTH
from pg_debug.health.checks.collation import COLLATION_HEALTH
from pg_debug.health.checks.multixact import MULTIXACT_HEALTH
from pg_debug.health.checks.multixact import MultixactCalc
from pg_debug.health.checks.slots import SLOT_RETENTION


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    return test_postgres_connection_string[0]


def server_version(url: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
        assert row is not None
        return int(row[0]) // 10000


def in_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest.fixture
def inactive_slot(url):
    """A physical slot that reserved WAL and has no consumer: what a standby leaves behind."""
    name = f"pgdebug_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_create_physical_replication_slot(%s, true)", [name])
    try:
        yield name
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = %s", [name])


class TestSlotRetention:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("access_mode", ["unrestricted", "restricted"])
    async def test_an_inactive_slot_is_reported_with_the_wal_it_retains(self, url, inactive_slot, access_mode):
        with psycopg.connect(url) as conn:
            row = conn.execute("SELECT current_setting('max_slot_wal_keep_size', true)").fetchone()
        keep = row[0] if row else None

        async with PgDebug(url, access_mode=access_mode) as pg:
            result = (await pg.analyze_db_health("replication"))[SLOT_RETENTION]

        assert result.error is None
        mine = [f for f in result.findings if f["slot_name"] == inactive_slot]
        assert len(mine) == 1, result.summary
        finding = mine[0]
        # Unbounded unless the server sets a bound; either way it is reported.
        assert finding["kind"] == ("inactive" if keep not in (None, "-1") else "unbounded")
        assert finding["active"] is False
        assert finding["retained_bytes"] is not None and finding["retained_bytes"] >= 0
        assert finding["confidence"] == "proven"
        assert inactive_slot in result.summary
        assert {"check": SLOT_RETENTION, "slot_name": inactive_slot, "kind": finding["kind"]} in health_keys({SLOT_RETENTION: result})

    @pytest.mark.asyncio
    async def test_after_the_slot_is_dropped_it_is_not_reported(self, url):
        name = f"pgdebug_test_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT pg_create_physical_replication_slot(%s, true)", [name])
            conn.execute("SELECT pg_drop_replication_slot(%s)", [name])

        async with PgDebug(url) as pg:
            result = (await pg.analyze_db_health("replication"))[SLOT_RETENTION]

        assert name not in result.summary


@pytest.fixture
def multixact_table(url):
    """A table whose row two transactions share-locked at once, which takes a multixact."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS pgdebug_mxid")
        conn.execute("CREATE TABLE pgdebug_mxid (id int PRIMARY KEY)")
        conn.execute("INSERT INTO pgdebug_mxid VALUES (1)")
    with psycopg.connect(url) as first, psycopg.connect(url) as second:
        first.execute("SELECT * FROM pgdebug_mxid WHERE id = 1 FOR SHARE")
        second.execute("SELECT * FROM pgdebug_mxid WHERE id = 1 FOR SHARE")
        first.commit()
        second.commit()
    try:
        yield "pgdebug_mxid"
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS pgdebug_mxid")


class TestMultixact:
    @pytest.mark.asyncio
    async def test_a_table_with_a_multixact_has_an_age_and_is_flagged_past_the_alert_age(self, url, multixact_table):
        async with PgDebug(url) as pg:
            driver = await pg.sql_driver()
            # Every table created since the last multixact shares its age, so list them all.
            result = await MultixactCalc(driver, alert_age=1, max_tables=100_000).multixact_check()

        tables = [f for f in result.findings if f["kind"] == "table" and f["table"] == multixact_table]
        assert len(tables) == 1, result.summary
        assert tables[0]["mxid_age"] >= 1
        assert tables[0]["level"] == "approaching"
        assert tables[0]["confidence"] == "proven"
        assert "PostgreSQL publishes no member-space percentage" in result.summary
        # The test servers connect as a superuser, which may list pg_multixact.
        assert result.metrics["member_files"]["files"] >= 1
        if server_version(url) >= 13:
            assert result.metrics["member_slru"] is not None

    @pytest.mark.asyncio
    async def test_the_default_thresholds_leave_a_young_server_healthy_in_restricted_mode(self, url, multixact_table):
        async with PgDebug(url, access_mode="restricted") as pg:
            result = (await pg.analyze_db_health("vacuum"))[MULTIXACT_HEALTH]

        assert result.error is None
        assert result.healthy, result.summary
        assert "restricted mode does not run pg_ls_dir" in result.summary


class TestCollationVersions:
    @pytest.mark.asyncio
    async def test_a_drifted_collation_names_the_index_that_uses_it(self, url):
        """The recorded version is set by hand, as pg_upgrade sets it: the library then reports another."""
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS pgdebug_coll")
            conn.execute("DROP COLLATION IF EXISTS pgdebug_drifted")
            conn.execute("CREATE COLLATION pgdebug_drifted (provider = icu, locale = 'de-DE', version = '1.0')")
            conn.execute('CREATE TABLE pgdebug_coll (a text COLLATE pgdebug_drifted, b text COLLATE "C")')
            conn.execute("CREATE INDEX pgdebug_coll_a ON pgdebug_coll (a)")
            conn.execute("CREATE INDEX pgdebug_coll_b ON pgdebug_coll (b)")
        try:
            for access_mode in ("unrestricted", "restricted"):
                async with PgDebug(url, access_mode=access_mode) as pg:
                    result = (await pg.analyze_db_health("collation"))[COLLATION_HEALTH]

                assert result.error is None
                drifted = [f for f in result.findings if f["kind"] == "collation"]
                assert [f["collation"] for f in drifted] == ["pgdebug_drifted"], result.summary
                assert drifted[0]["recorded_version"] == "1.0"
                assert [i["index"] for i in drifted[0]["indexes"]] == ["public.pgdebug_coll_a"]
                assert drifted[0]["fix"][-1] == "ALTER COLLATION public.pgdebug_drifted REFRESH VERSION;"
                assert {"check": COLLATION_HEALTH, "schema": "public", "collation": "pgdebug_drifted", "kind": "collation"} in health_keys(
                    {COLLATION_HEALTH: result}
                )
        finally:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute("DROP TABLE IF EXISTS pgdebug_coll")
                conn.execute("DROP COLLATION IF EXISTS pgdebug_drifted")

    @pytest.mark.asyncio
    async def test_a_database_whose_default_collation_drifted_is_reported(self, url):
        if server_version(url) < 15:
            pytest.skip("a database's collation version is recorded from PostgreSQL 15")
        name = f"pgdebug_coll_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                f"CREATE DATABASE {name} TEMPLATE template0 LOCALE_PROVIDER icu ICU_LOCALE 'en-US' LOCALE 'C.UTF-8' COLLATION_VERSION '1.0'"  # type: ignore[arg-type]
            )
        try:
            with psycopg.connect(in_database(url, name), autocommit=True) as conn:
                conn.execute("CREATE TABLE t (a text)")
                conn.execute("CREATE INDEX t_a ON t (a)")

            async with PgDebug(in_database(url, name)) as pg:
                inside = (await pg.analyze_db_health("collation"))[COLLATION_HEALTH]
            async with PgDebug(url) as pg:
                outside = (await pg.analyze_db_health("collation"))[COLLATION_HEALTH]

            mine = [f for f in inside.findings if f["kind"] == "database" and f["database"] == name]
            assert len(mine) == 1, inside.summary
            assert [i["index"] for i in mine[0]["indexes"]] == ["public.t_a"]
            assert mine[0]["fix"] == ["REINDEX INDEX CONCURRENTLY public.t_a;", f"ALTER DATABASE {name} REFRESH COLLATION VERSION;"]

            seen = [f for f in outside.findings if f["kind"] == "database" and f["database"] == name]
            assert len(seen) == 1 and seen[0]["indexes"] is None
        finally:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")  # type: ignore[arg-type]


class TestChecksums:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("access_mode", ["unrestricted", "restricted"])
    async def test_the_check_says_whether_failures_can_be_counted(self, url, access_mode):
        with psycopg.connect(url) as conn:
            row = conn.execute("SHOW data_checksums").fetchone()
        enabled = bool(row and row[0] == "on")

        async with PgDebug(url, access_mode=access_mode) as pg:
            result = (await pg.analyze_db_health("checksum"))[CHECKSUM_HEALTH]

        assert result.error is None
        assert result.metrics["data_checksums"] is enabled
        if not enabled:
            assert result.healthy
            assert "Data checksums are off" in result.summary
        elif server_version(url) >= 12:
            # Nothing corrupts a page in a test; the counters are read and found zero.
            assert result.metrics["databases_examined"] >= 1
