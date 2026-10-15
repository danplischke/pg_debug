"""Shard readiness: "not yet" by default, and every refusal with the figure behind it.

The assessor is driven by a stubbed server here; the container-backed test
runs it against a real one. Most tests are about the refusals, because the
answer is "not yet" far more often than not, and a refusal without its number
is an opinion.
"""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.db import reset_postgres_version_cache
from pg_debug.errors import InvalidArgumentError
from pg_debug.partition import PartitionCandidate
from pg_debug.partition import Strategy
from pg_debug.sharding import RefusalReason
from pg_debug.sharding import ShardReadinessAssessor
from pg_debug.sharding import Verdict
from pg_debug.sharding.assessor import lsn_bytes
from pg_debug.sharding.assessor import routing_keys
from pg_debug.sharding.assessor import workload_figures

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)

Answer = list[dict[str, Any]] | Callable[[], list[dict[str, Any]]]


def workload(*queries: tuple[str, float]) -> list[dict[str, Any]]:
    return [{"query": query, "weight": weight, "calls": 1} for query, weight in queries]


def snapshots(*rows: dict[str, Any]) -> Callable[[], list[dict[str, Any]]]:
    """Answer with each row in turn: the snapshot before the interval, then after."""
    remaining = list(rows)

    def answer() -> list[dict[str, Any]]:
        return [remaining.pop(0) if len(remaining) > 1 else remaining[0]]

    return answer


class Server:
    """A driver answering each statement by the first fragment it contains."""

    def __init__(self, answers: dict[str, Answer]):
        self.answers = answers
        self.queries: list[str] = []

    async def execute_query(self, query, params=None, force_readonly=False, *, in_context=False, cleanup=None):
        self.queries.append(query)
        for fragment, answer in self.answers.items():
            if fragment in query:
                rows = answer() if callable(answer) else answer
                return [SqlDriver.RowResult(cells=row) for row in rows]
        return []


def server(
    version: int = 16,
    statements: list[dict[str, Any]] | None = None,
    total_time: float | None = None,
    checkpoints: tuple[int, int] = (100, 3),
    in_recovery: bool = False,
    citus: bool = False,
    extra: dict[str, Answer] | None = None,
) -> Server:
    statements = statements if statements is not None else []
    timed, requested = checkpoints
    answers: dict[str, Answer] = {
        **(extra or {}),
        "server_version_num": [{"server_version_num": str(version * 10000 + 4)}],
        "pg_is_in_recovery": [{"in_recovery": in_recovery, "database_bytes": 5 * 1024**3}],
        "FROM pg_stat_wal": snapshots(
            {"wal_bytes": 1_000, "wal_records": 1, "stats_reset": NOW - timedelta(days=3), "now": NOW},
            {"wal_bytes": 11_000, "wal_records": 9, "stats_reset": NOW - timedelta(days=3), "now": NOW + timedelta(seconds=10)},
        ),
        "pg_current_wal_lsn": snapshots(
            {"lsn": "0/1000", "now": NOW},
            {"lsn": "0/3000", "now": NOW + timedelta(seconds=2)},
        ),
        "FROM pg_stat_io": snapshots({"evictions": 10, "reads": 100, "hits": 5}, {"evictions": 60, "reads": 300, "hits": 9}),
        "AS timed": [{"timed": timed, "requested": requested}],
        "server_reset": [{"server_reset": NOW - timedelta(days=3), "now": NOW}],
        "autovacuum_vacuum_threshold": [],
        "extname = 'citus'": [{"version": "13.0", "stat_tenants_track": "none"}] if citus else [],
        "FROM pg_extension e": [{"schema": "public", "version": "1.10"}] if statements else [],
        "pg_stat_statements_info": [{"stats_reset": NOW - timedelta(days=2), "dealloc": 0, "now": NOW}],
        "count(*) AS statements": [
            {"total_time": total_time if total_time is not None else sum(s["weight"] for s in statements), "statements": len(statements)}
        ],
        "SELECT query, calls,": [{"query": s["query"], "calls": 1, "total_exec_time": s["weight"]} for s in statements],
    }
    return Server(answers)


async def assess(server: Server, restricted: bool = False, interval: float = 10):
    reset_postgres_version_cache()
    driver: Any = SafeSqlDriver(sql_driver=server, timeout=None) if restricted else server  # type: ignore[arg-type]
    with patch("pg_debug.sharding.assessor.asyncio.sleep", AsyncMock()):
        return await ShardReadinessAssessor(driver).assess(interval)


def reasons(readiness) -> set[RefusalReason]:
    return {refusal.reason for refusal in readiness.refusals}


#: Writes keyed by tenant_id, spread over enough statements that no five dominate.
TENANT_WRITES = workload(*((f"UPDATE orders SET total = $1 WHERE tenant_id = $2 AND total > {i}", 10.0) for i in range(20)))


# -- The arithmetic ------------------------------------------------------------


def test_a_routing_key_is_weighed_by_the_time_that_compares_it_by_equality():
    keys = routing_keys(
        workload(
            ("SELECT * FROM orders WHERE tenant_id = $1", 60.0),
            ("SELECT * FROM orders o JOIN accounts a ON a.id = o.account_id WHERE a.tenant_id = $1", 20.0),
            ("SELECT * FROM orders WHERE created_at > $1", 20.0),
        )
    )

    assert keys[0] == {"column": "tenant_id", "share": 0.8, "tables": ["accounts", "orders"]}
    assert "created_at" not in [key["column"] for key in keys], "a range predicate does not route to one shard"


def test_a_statement_on_the_catalog_alone_is_not_part_of_the_workload_a_key_is_judged_by():
    keys = routing_keys(workload(("SELECT * FROM pg_class WHERE oid = $1", 1000.0), ("SELECT * FROM orders WHERE tenant_id = $1", 1.0)))

    assert keys == [{"column": "tenant_id", "share": 1.0, "tables": ["orders"]}]


def test_an_insert_of_values_is_routed_by_the_columns_it_names():
    keys = routing_keys(workload(("INSERT INTO orders (tenant_id, total) VALUES ($1, $2)", 1.0)))

    assert {"column": "tenant_id", "share": 1.0, "tables": ["orders"]} in keys


def test_reads_are_selects_that_lock_and_write_nothing():
    figures = workload_figures(
        workload(
            ("SELECT * FROM orders WHERE id = $1", 50.0),
            ("SELECT * FROM orders WHERE id = $1 FOR UPDATE", 20.0),
            ("WITH gone AS (DELETE FROM orders WHERE id = $1 RETURNING *) SELECT * FROM gone", 30.0),
        ),
        total_time=100.0,
        statements=3,
    )

    assert figures["read_share"] == 0.5


def test_the_top_share_is_of_all_the_recorded_time_not_only_the_statements_read():
    figures = workload_figures(workload(*((f"SELECT {i}", 10.0) for i in range(5))), total_time=200.0, statements=400)

    assert figures["top_share"] == 0.25
    assert figures["time_read_share"] == 0.25


def test_an_lsn_is_a_byte_position():
    assert lsn_bytes("16/B374D848") == (0x16 << 32) + 0xB374D848
    assert lsn_bytes("0/3000") - lsn_bytes("0/1000") == 0x2000


# -- The verdict ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_quiet_server_is_not_yet_with_the_figures_behind_it():
    readiness = await assess(server(statements=TENANT_WRITES))

    assert readiness.verdict is Verdict.NOT_YET
    assert reasons(readiness) == {RefusalReason.HEALTHY_WRITE_RATE}
    refusal = readiness.refusals[0]
    assert refusal.figures["checkpoints_requested"] == 3
    assert refusal.figures["wal_bytes_per_second"] == 1000
    assert refusal.changes_if in readiness.would_change


@pytest.mark.asyncio
async def test_a_pressed_write_path_with_a_covering_key_is_worth_evaluating_and_nothing_more():
    readiness = await assess(server(statements=TENANT_WRITES, checkpoints=(40, 60)), restricted=True)

    assert readiness.verdict is Verdict.WORTH_EVALUATING
    assert readiness.refusals == []
    assert "60 of 100 checkpoints" in readiness.pressure[0]
    assert readiness.routing_keys[0]["column"] == "tenant_id"
    assert "not a verdict to shard" in readiness.summary


@pytest.mark.asyncio
async def test_a_read_load_is_refused_in_favour_of_replicas():
    reads = workload(*((f"SELECT * FROM orders WHERE tenant_id = $1 AND id = {i}", 10.0) for i in range(20)))

    readiness = await assess(server(statements=reads, checkpoints=(40, 60)))

    refusal = next(r for r in readiness.refusals if r.reason is RefusalReason.READ_LOAD)
    assert refusal.figures["read_share"] == 1.0
    assert "replicas" in refusal.instead


@pytest.mark.asyncio
async def test_a_few_statements_taking_most_of_the_time_are_tuned_first():
    heavy = [*workload(("UPDATE orders SET total = 0 WHERE tenant_id = $1", 900.0)), *TENANT_WRITES]

    readiness = await assess(server(statements=heavy, checkpoints=(40, 60)))

    refusal = next(r for r in readiness.refusals if r.reason is RefusalReason.FEW_QUERIES_DOMINATE)
    assert refusal.figures["top_share"] >= 0.5
    assert "top-queries" in refusal.instead


@pytest.mark.asyncio
async def test_no_column_covering_most_of_the_time_is_no_routing_key():
    scattered = workload(*((f"UPDATE orders SET total = 0 WHERE col_{i} = $1", 10.0) for i in range(20)))

    readiness = await assess(server(statements=scattered, checkpoints=(40, 60)))

    refusal = next(r for r in readiness.refusals if r.reason is RefusalReason.NO_ROUTING_KEY)
    assert refusal.figures["best_share"] == 0.05


@pytest.mark.asyncio
async def test_without_pg_stat_statements_there_is_no_workload_to_judge_by():
    readiness = await assess(server(statements=[], checkpoints=(40, 60)))

    assert RefusalReason.NO_WORKLOAD in reasons(readiness)
    assert any("pg_stat_statements is not installed" in note for note in readiness.notes)


@pytest.mark.asyncio
async def test_data_deleted_by_time_is_a_partitioning_problem():
    candidate = PartitionCandidate(
        schema="public",
        table="events",
        table_bytes=200 * 1024**3,
        row_count=10**9,
        column="created_at",
        column_type="timestamptz",
        strategy=Strategy.RANGE,
        pruning_share=0.9,
        recommended=True,
        evidence={"deletes_filter_on_this_column": True},
    )

    with patch("pg_debug.sharding.assessor.PartitionAdvisor.analyze", AsyncMock(return_value=[candidate])):
        readiness = await assess(server(statements=TENANT_WRITES, checkpoints=(40, 60)))

    refusal = next(r for r in readiness.refusals if r.reason is RefusalReason.RETENTION)
    assert refusal.figures == {
        "table": "public.events",
        "table_bytes": 200 * 1024**3,
        "column": "created_at",
        "pruning_share": 0.9,
        "deletes_filter_on_this_column": True,
    }
    assert readiness.verdict is Verdict.NOT_YET


# -- Version gates -------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_14_the_wal_rate_comes_from_the_wal_position():
    srv = server(version=13, statements=TENANT_WRITES)

    readiness = await assess(srv)

    wal = readiness.measurements["wal"]
    assert wal["source"] == "pg_current_wal_lsn()"
    assert wal["bytes_in_interval"] == 0x2000 and wal["bytes_per_second"] == 0x1000
    assert not any("pg_stat_wal" in query for query in srv.queries)
    assert not any("pg_stat_io" in query for query in srv.queries), "pg_stat_io arrived in 16"
    assert readiness.measurements["io"] == {"source": None}


@pytest.mark.asyncio
async def test_a_standby_before_14_says_the_wal_rate_could_not_be_measured():
    srv = server(version=13, statements=TENANT_WRITES, in_recovery=True)

    readiness = await assess(srv)

    assert readiness.measurements["wal"] == {"source": None}
    assert not any("pg_current_wal_lsn" in query for query in srv.queries), "it refuses to run during recovery"
    assert any("standby" in note for note in readiness.notes)


@pytest.mark.asyncio
async def test_from_17_the_checkpoints_come_from_the_checkpointer():
    srv = server(version=17, statements=TENANT_WRITES)

    await assess(srv)

    assert any("pg_stat_checkpointer" in query and "num_requested" in query for query in srv.queries)
    assert not any("checkpoints_req" in query for query in srv.queries)


@pytest.mark.asyncio
async def test_from_16_evictions_are_measured_over_the_interval():
    readiness = await assess(server(statements=TENANT_WRITES))

    assert readiness.measurements["io"] == {"source": "pg_stat_io", "evictions_in_interval": 50, "reads_in_interval": 200, "hits_in_interval": 4}


# -- Arguments and Citus -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_interval_that_is_not_positive_is_refused():
    with pytest.raises(InvalidArgumentError):
        await ShardReadinessAssessor(Server({})).assess(0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_citus_is_read_only_where_it_is_installed():
    without = await assess(server(statements=TENANT_WRITES))
    with_citus = await assess(server(statements=TENANT_WRITES, citus=True), restricted=True)

    assert without.citus is None
    assert with_citus.citus is not None and with_citus.citus["version"] == "13.0"
    assert with_citus.to_dict()["citus"]["findings"][0]["check"] == "tenant_tracking_off"


@pytest.mark.parametrize("version", [13, 16, 17])
@pytest.mark.asyncio
async def test_every_statement_passes_restricted_mode_on_every_version_path(version):
    readiness = await assess(server(version=version, statements=TENANT_WRITES, citus=True), restricted=True)

    assert readiness.verdict is Verdict.NOT_YET
