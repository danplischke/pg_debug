"""What a Citus coordinator's own views say about how it is sharded.

Citus is not installed where these tests run, so its views are stubbed with
the columns its documentation lists. Every statement passes restricted mode.
"""

from typing import Any

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import QueryError
from pg_debug.severity import Confidence
from pg_debug.severity import Severity
from pg_debug.sharding.citus import assess_citus
from pg_debug.sharding.citus import distribution_columns
from pg_debug.sharding.citus import hot_shards
from pg_debug.sharding.citus import keys_without_distribution_column
from pg_debug.sharding.citus import multi_shard
from pg_debug.sharding.citus import node_skew
from pg_debug.sharding.citus import tenant_share


class Server:
    def __init__(self, answers: dict[str, Any]):
        self.answers = answers

    async def execute_query(self, query, params=None, force_readonly=False, *, in_context=False, cleanup=None):
        for fragment, answer in self.answers.items():
            if fragment in query:
                if isinstance(answer, Exception):
                    raise answer
                return [SqlDriver.RowResult(cells=row) for row in answer]
        return []


def checks(findings) -> list[str]:
    return [finding.check for finding in findings]


@pytest.mark.asyncio
async def test_without_the_extension_there_is_nothing_to_read():
    assert await assess_citus(Server({})) is None  # type: ignore[arg-type]


def test_a_node_holding_twice_the_bytes_of_another_is_skew():
    evidence: dict[str, Any] = {}
    nodes = [{"nodename": "w1", "nodeport": 5432, "bytes": 300}, {"nodename": "w2", "nodeport": 5432, "bytes": 100}]

    [finding] = node_skew(nodes, evidence)

    assert finding.evidence["ratio"] == 3.0
    assert evidence["node_bytes"] == {"w1:5432": 300, "w2:5432": 100}
    assert node_skew([{**nodes[0], "bytes": 150}, nodes[1]], {}) == []


def test_a_shard_three_times_its_tables_average_is_a_hot_shard():
    [finding] = hot_shards([{"table_name": "orders", "largest": 900, "average": 200, "shards": 32}])

    assert finding.check == "hot_shard" and "isolate_tenant_to_new_shard" in finding.summary
    assert hot_shards([{"table_name": "orders", "largest": 300, "average": 200, "shards": 32}]) == []


def test_the_top_tenants_share_is_said_to_be_of_the_tracked_tenants():
    evidence: dict[str, Any] = {}

    [finding] = tenant_share([{"tenant_attribute": "42", "cpu": 6.0, "tracked_cpu": 10.0}], evidence)

    assert evidence["top_tenant_cpu_share"] == 0.6
    assert "only the top tenants" in finding.summary
    assert tenant_share([{"tenant_attribute": "42", "cpu": 1.0, "tracked_cpu": 10.0}], {}) == []


def test_calls_without_a_partition_key_are_a_candidate_with_a_way_to_confirm():
    evidence: dict[str, Any] = {}

    [finding] = multi_shard([{"calls": 100, "without_key": 70}], evidence)

    assert finding.confidence is Confidence.CANDIDATE and finding.verify
    assert evidence["calls_without_partition_key_share"] == 0.7
    assert multi_shard([{"calls": 100, "without_key": 10}], {}) == []


def test_a_timestamp_distribution_column_is_proven_from_the_catalog():
    [finding] = distribution_columns([{"table_name": "events", "distribution_column": "created_at", "shard_count": 32, "type_name": "timestamptz"}])

    assert finding.check == "timestamp_distribution_column"
    assert (finding.severity, finding.confidence) == (Severity.HIGH, Confidence.PROVEN)


def test_fewer_distinct_values_than_shards_is_a_sampled_candidate():
    [finding] = distribution_columns(
        [{"table_name": "orders", "distribution_column": "region", "shard_count": 32, "type_name": "text", "n_distinct": 4}]
    )

    assert finding.check == "low_cardinality_distribution_column"
    assert finding.confidence is Confidence.CANDIDATE
    assert finding.verify == "SELECT count(DISTINCT region) FROM orders"
    # A negative n_distinct is a fraction of the rows, so not a small number of values.
    assert distribution_columns([{"table_name": "o", "distribution_column": "id", "shard_count": 32, "type_name": "int8", "n_distinct": -1}]) == []


def test_keys_without_the_distribution_column_are_named_except_a_key_to_a_reference_table():
    keys = [
        {
            "table_name": "orders",
            "distribution_column": "tenant_id",
            "kind": "unique",
            "name": "orders_pkey",
            "columns": ["id"],
            "references_type": None,
        },
        {
            "table_name": "orders",
            "distribution_column": "tenant_id",
            "kind": "unique",
            "name": "orders_tenant_id_id_key",
            "columns": ["tenant_id", "id"],
            "references_type": None,
        },
        {
            "table_name": "orders",
            "distribution_column": "tenant_id",
            "kind": "foreign key",
            "name": "orders_account_fkey",
            "columns": ["account_id"],
            "references_type": "distributed",
        },
        {
            "table_name": "orders",
            "distribution_column": "tenant_id",
            "kind": "foreign key",
            "name": "orders_country_fkey",
            "columns": ["country"],
            "references_type": "reference",
        },
    ]

    assert checks(keys_without_distribution_column(keys)) == ["unique_key_without_distribution_column", "foreign_key_without_distribution_column"]


@pytest.mark.asyncio
async def test_a_missing_view_is_reported_and_the_others_still_read_in_restricted_mode():
    server = Server(
        {
            "extname = 'citus'": [{"version": "11.2", "stat_tenants_track": "all"}],
            "citus_stat_tenants": QueryError('relation "citus_stat_tenants" does not exist'),
            "GROUP BY nodename": [
                {"nodename": "w1", "nodeport": 5432, "bytes": 400, "shards": 16},
                {"nodename": "w2", "nodeport": 5432, "bytes": 100, "shards": 16},
            ],
            "citus_stat_statements": [{"calls": 10, "without_key": 9}],
        }
    )

    result = await assess_citus(SafeSqlDriver(sql_driver=server, timeout=None))  # type: ignore[arg-type]

    assert result is not None
    assert "citus_stat_tenants" in result["errors"]["top_tenant"]
    assert checks(result["findings"])[:1] == ["multi_shard_calls"], "high severity first"
    assert "shard_size_skew" in checks(result["findings"])
