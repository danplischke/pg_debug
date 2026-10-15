"""Shard readiness and kept history against a real server.

The workload is a multi-tenant one: every statement filters on tenant_id. The
platform readers cannot be tried here; what a real server shows is the
extension readers saying what is missing, and that everything runs in
restricted mode.
"""

import asyncio

import psycopg
import pytest

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug.sharding import RefusalReason
from pg_debug.sharding import Verdict

SETUP = [
    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements",
    "DROP TABLE IF EXISTS shard_orders, shard_accounts",
    "CREATE TABLE shard_accounts (id bigint PRIMARY KEY, tenant_id int NOT NULL, name text)",
    "CREATE TABLE shard_orders (id bigserial PRIMARY KEY, tenant_id int NOT NULL, account_id bigint, total numeric)",
    "INSERT INTO shard_accounts SELECT g, g % 50, 'a' || g FROM generate_series(1, 2000) g",
    "INSERT INTO shard_orders (tenant_id, account_id, total) SELECT g % 50, g % 2000, g FROM generate_series(1, 5000) g",
    "ANALYZE shard_accounts, shard_orders",
]

WORKLOAD = [
    "SELECT count(*) FROM shard_orders WHERE tenant_id = {t}",
    "UPDATE shard_accounts SET name = 'x' WHERE tenant_id = {t} AND id = {t}",
    "SELECT o.total FROM shard_orders o JOIN shard_accounts a ON a.id = o.account_id WHERE o.tenant_id = {t} AND a.tenant_id = {t}",
    "INSERT INTO shard_orders (tenant_id, account_id, total) VALUES ({t}, {t}, 1)",
]


@pytest.fixture
def url(test_postgres_connection_string) -> str:
    url = test_postgres_connection_string[0]
    with psycopg.connect(url, autocommit=True) as conn:
        for statement in SETUP:
            conn.execute(statement)  # type: ignore[arg-type]
        # Enough repetitions that the setup's bulk loads are not the top statements.
        for round_ in range(300):
            for statement in WORKLOAD:
                conn.execute(statement.format(t=round_ % 50))  # type: ignore[arg-type]
    return url


async def _write_during(url: str, seconds: float) -> None:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            await conn.execute("INSERT INTO shard_orders (tenant_id, account_id, total) VALUES (1, 1, 1)")


@pytest.mark.asyncio
@pytest.mark.parametrize("access_mode", [AccessMode.UNRESTRICTED, AccessMode.RESTRICTED])
async def test_a_tenant_workload_is_not_yet_and_names_its_routing_key(url, access_mode):
    async with PgDebug(url, access_mode=access_mode) as pg:
        readiness, _ = await asyncio.gather(pg.assess_shard_readiness(interval_seconds=1.5), _write_during(url, 1.0))

    assert readiness.verdict is Verdict.NOT_YET
    reasons = {refusal.reason for refusal in readiness.refusals}
    assert RefusalReason.NO_ROUTING_KEY not in reasons, readiness.routing_keys
    assert readiness.routing_keys[0]["column"] == "tenant_id"
    assert readiness.routing_keys[0]["tables"] == ["shard_accounts", "shard_orders"]

    measured = readiness.measurements
    version = _version(url)
    assert measured["wal"]["source"] == ("pg_stat_wal" if version >= 14 else "pg_current_wal_lsn()")
    assert measured["wal"]["bytes_in_interval"] > 0, "the writer ran during the interval"
    assert measured["io"]["source"] == ("pg_stat_io" if version >= 16 else None)
    assert measured["checkpoints"]["counting_since"] is not None
    assert measured["workload"]["statements_read"] >= len(WORKLOAD)
    # pg_stat_statements records its own reset from 1.9, which came with PostgreSQL 14.
    assert (measured["workload"]["counting_since"] is not None) == (version >= 14)
    assert readiness.citus is None


@pytest.mark.asyncio
async def test_every_refusal_and_every_pressure_carries_its_figure(url):
    """Whether the server shows pressure depends on what else ran on it; either way the answer has numbers."""
    async with PgDebug(url, access_mode=AccessMode.RESTRICTED) as pg:
        readiness = await pg.assess_shard_readiness(interval_seconds=0.5)

    assert readiness.refusals, "this workload's top statements are few, so something refuses"
    for refusal in readiness.refusals:
        assert refusal.figures and any(isinstance(value, (int, float)) for value in refusal.figures.values()), refusal
        assert refusal.changes_if in readiness.would_change
    for line in readiness.pressure:
        assert any(ch.isdigit() for ch in line)
    healthy = [r for r in readiness.refusals if r.reason is RefusalReason.HEALTHY_WRITE_RATE]
    assert bool(healthy) != bool(readiness.pressure), "the write path is either pressed or refused as healthy"
    if healthy:
        assert healthy[0].figures["database_bytes"] > 0


def _version(url: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    return int(row[0]) // 10000


@pytest.mark.asyncio
async def test_history_says_what_is_missing_in_restricted_mode(url):
    async with PgDebug(url, access_mode=AccessMode.RESTRICTED) as pg:
        report = await pg.read_history()

    by_name = {source.name: source for source in report.sources}
    assert {"pg_qualstats", "pg_wait_sampling"} <= set(by_name)
    for name in ("pg_qualstats", "pg_wait_sampling"):
        source = by_name[name]
        if source.present:
            continue
        assert source.error is None
        assert "Not installed" in source.status
        assert source.evidence["available_on_server"] in (True, False)
    assert report.platform["platform"] == "unknown"
