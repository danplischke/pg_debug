"""What a Citus cluster's own views say about how well it is sharded.

Read on the coordinator, from the views Citus documents
(https://docs.citusdata.com/en/stable/develop/api_metadata.html):
``citus_tables`` (table_name, citus_table_type, distribution_column,
shard_count), ``citus_shards`` (table_name, shardid, nodename, nodeport,
shard_size), ``citus_stat_tenants`` (tenant_attribute,
cpu_usage_in_last_period, which needs ``citus.stat_tenants_track = 'all'``)
and ``citus_stat_statements`` (partition_key, calls).

Citus is not installed where pg-debug is tested, so this follows the
documentation and is tested against stubbed rows only.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..errors import PgDebugError
from ..severity import Confidence
from ..severity import Severity
from .result import CitusFinding

logger = logging.getLogger(__name__)

#: The largest node holding this many times the bytes of the smallest is skew
#: worth rebalancing.
NODE_SKEW_RATIO = 2.0

#: A shard this many times its table's average is a hot shard: usually one
#: large tenant.
HOT_SHARD_RATIO = 3.0

#: One tenant taking this share of the CPU the tracked tenants used.
TENANT_CPU_SHARE = 0.5

#: This share of calls with no distribution key fan out to every shard.
MULTI_SHARD_SHARE = 0.5

_TIME_TYPES = frozenset({"date", "timestamp", "timestamptz", "time", "timetz"})

PRESENCE_SQL = """
SELECT extversion AS version, current_setting('citus.stat_tenants_track', true) AS stat_tenants_track
FROM pg_extension WHERE extname = 'citus'
"""

NODE_SIZES_SQL = """
SELECT nodename, nodeport, sum(shard_size) AS bytes, count(*) AS shards
FROM citus_shards
WHERE citus_table_type = 'distributed'
GROUP BY nodename, nodeport
ORDER BY sum(shard_size) DESC
"""

HOT_SHARDS_SQL = """
SELECT table_name::text AS table_name, max(shard_size) AS largest, avg(shard_size) AS average, count(*) AS shards
FROM citus_shards
WHERE citus_table_type = 'distributed'
GROUP BY table_name
"""

TOP_TENANT_SQL = """
SELECT tenant_attribute, cpu_usage_in_last_period AS cpu, sum(cpu_usage_in_last_period) OVER () AS tracked_cpu
FROM citus_stat_tenants
ORDER BY cpu_usage_in_last_period DESC
LIMIT 1
"""

MULTI_SHARD_SQL = """
SELECT sum(calls) AS calls, sum(CASE WHEN partition_key IS NULL THEN calls ELSE 0 END) AS without_key
FROM citus_stat_statements
"""

DISTRIBUTION_COLUMNS_SQL = """
SELECT t.table_name::text AS table_name,
       t.distribution_column,
       t.shard_count,
       ty.typname AS type_name,
       s.n_distinct
FROM citus_tables t
JOIN pg_class c ON c.oid = t.table_name
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = t.distribution_column
JOIN pg_type ty ON ty.oid = a.atttypid
LEFT JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname AND s.attname = a.attname AND NOT s.inherited
WHERE t.citus_table_type = 'distributed'
"""

# Unique indexes and foreign keys of distributed tables, with their columns,
# and for a foreign key whether the table it references is distributed too: a
# key to a reference table needs no distribution column.
KEYS_SQL = """
SELECT t.table_name::text AS table_name,
       t.distribution_column,
       'unique' AS kind,
       ic.relname AS name,
       (SELECT array_agg(a.attname::text ORDER BY k.ord)
          FROM unnest((i.indkey::int2[])[0:i.indnkeyatts - 1]) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum) AS columns,
       NULL::text AS references_type
FROM citus_tables t
JOIN pg_index i ON i.indrelid = t.table_name AND i.indisunique
JOIN pg_class ic ON ic.oid = i.indexrelid
WHERE t.citus_table_type = 'distributed'
UNION ALL
SELECT t.table_name::text,
       t.distribution_column,
       'foreign key',
       con.conname::text,
       (SELECT array_agg(a.attname::text ORDER BY k.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum),
       r.citus_table_type
FROM citus_tables t
JOIN pg_constraint con ON con.conrelid = t.table_name AND con.contype = 'f'
LEFT JOIN citus_tables r ON r.table_name = con.confrelid
WHERE t.citus_table_type = 'distributed'
"""


async def _rows(sql_driver: SqlDriver | SafeSqlDriver, sql: Any) -> list[dict[str, Any]]:
    rows = await SafeSqlDriver.execute_param_query(sql_driver, sql)
    return [dict(row.cells) for row in rows or []]


async def assess_citus(sql_driver: SqlDriver | SafeSqlDriver) -> dict[str, Any] | None:
    """Read the cluster's own views, or None where the citus extension is not installed.

    Returns:
        ``version``, ``findings`` (:class:`CitusFinding`), ``evidence`` and
        ``errors``: the reads the server refused, by name, so that one
        missing view (``citus_stat_tenants`` before Citus 11.3) does not
        hide the others.
    """
    presence = await _rows(sql_driver, PRESENCE_SQL)
    if not presence:
        return None
    version, track = presence[0].get("version"), presence[0].get("stat_tenants_track")

    findings: list[CitusFinding] = []
    evidence: dict[str, Any] = {"citus.stat_tenants_track": track}
    errors: dict[str, str] = {}

    async def read(name: str, sql: Any) -> list[dict[str, Any]] | None:
        try:
            return await _rows(sql_driver, sql)
        except PgDebugError as e:
            logger.info("Could not read %s: %s", name, e)
            errors[name] = str(e)
            return None

    nodes = await read("node_sizes", NODE_SIZES_SQL)
    if nodes is not None:
        findings.extend(node_skew(nodes, evidence))
    shards = await read("hot_shards", HOT_SHARDS_SQL)
    if shards is not None:
        findings.extend(hot_shards(shards))
    if (track or "").lower() == "all":
        tenants = await read("top_tenant", TOP_TENANT_SQL)
        if tenants is not None:
            findings.extend(tenant_share(tenants, evidence))
    else:
        findings.append(
            CitusFinding(
                check="tenant_tracking_off",
                severity=Severity.LOW,
                confidence=Confidence.PROVEN,
                summary=(
                    f"citus.stat_tenants_track is {track or 'not set'}, so citus_stat_tenants records no tenant's CPU: "
                    "set it to 'all' to see whether one tenant dominates."
                ),
                evidence={"citus.stat_tenants_track": track},
            )
        )
    calls = await read("multi_shard_calls", MULTI_SHARD_SQL)
    if calls is not None:
        findings.extend(multi_shard(calls, evidence))
    columns = await read("distribution_columns", DISTRIBUTION_COLUMNS_SQL)
    if columns is not None:
        findings.extend(distribution_columns(columns))
    keys = await read("keys", KEYS_SQL)
    if keys is not None:
        findings.extend(keys_without_distribution_column(keys))

    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    findings.sort(key=lambda finding: order[finding.severity])
    return {"version": version, "findings": findings, "evidence": evidence, "errors": errors}


def node_skew(nodes: list[dict[str, Any]], evidence: dict[str, Any]) -> list[CitusFinding]:
    sizes = [int(node.get("bytes") or 0) for node in nodes]
    evidence["node_bytes"] = {f"{node['nodename']}:{node['nodeport']}": int(node.get("bytes") or 0) for node in nodes}
    if len(sizes) < 2 or min(sizes) <= 0:
        return []
    ratio = max(sizes) / min(sizes)
    if ratio < NODE_SKEW_RATIO:
        return []
    largest, smallest = nodes[0], nodes[-1]
    return [
        CitusFinding(
            check="shard_size_skew",
            severity=Severity.MEDIUM,
            confidence=Confidence.PROVEN,
            summary=(
                f"Node {largest['nodename']}:{largest['nodeport']} holds {ratio:.1f} times the shard bytes of "
                f"{smallest['nodename']}:{smallest['nodeport']} ({max(sizes):,} against {min(sizes):,}). "
                "citus_rebalance_start() moves shards by size."
            ),
            evidence={"ratio": round(ratio, 2), "largest_bytes": max(sizes), "smallest_bytes": min(sizes)},
        )
    ]


def hot_shards(shards: list[dict[str, Any]]) -> list[CitusFinding]:
    findings = []
    for table in shards:
        largest, average = float(table.get("largest") or 0), float(table.get("average") or 0)
        if average <= 0 or int(table.get("shards") or 0) < 2:
            continue
        ratio = largest / average
        if ratio >= HOT_SHARD_RATIO:
            findings.append(
                CitusFinding(
                    check="hot_shard",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.PROVEN,
                    summary=(
                        f"{table['table_name']}'s largest shard is {ratio:.1f} times its average shard, usually one large "
                        "tenant: isolate_tenant_to_new_shard() gives it a shard of its own."
                    ),
                    evidence={"table": table["table_name"], "largest_bytes": int(largest), "average_bytes": round(average), "ratio": round(ratio, 2)},
                )
            )
    return findings


def tenant_share(tenants: list[dict[str, Any]], evidence: dict[str, Any]) -> list[CitusFinding]:
    if not tenants:
        return []
    top = tenants[0]
    cpu, tracked = float(top.get("cpu") or 0), float(top.get("tracked_cpu") or 0)
    if tracked <= 0:
        return []
    share = cpu / tracked
    evidence["top_tenant_cpu_share"] = round(share, 3)
    if share < TENANT_CPU_SHARE:
        return []
    return [
        CitusFinding(
            check="tenant_cpu_share",
            severity=Severity.MEDIUM,
            confidence=Confidence.PROVEN,
            summary=(
                f"Tenant {top['tenant_attribute']} used {share:.0%} of the CPU the tracked tenants used in the last period. "
                "citus_stat_tenants lists only the top tenants, so this is a share of theirs, not of the cluster's."
            ),
            evidence={"tenant": top["tenant_attribute"], "cpu_seconds": cpu, "tracked_cpu_seconds": tracked},
        )
    ]


def multi_shard(rows: list[dict[str, Any]], evidence: dict[str, Any]) -> list[CitusFinding]:
    cells = rows[0] if rows else {}
    calls, without = int(cells.get("calls") or 0), int(cells.get("without_key") or 0)
    if calls <= 0:
        return []
    share = without / calls
    evidence["calls_without_partition_key_share"] = round(share, 3)
    if share < MULTI_SHARD_SHARE:
        return []
    return [
        CitusFinding(
            check="multi_shard_calls",
            severity=Severity.HIGH,
            confidence=Confidence.CANDIDATE,
            summary=(
                f"{share:.0%} of the calls in citus_stat_statements ({without:,} of {calls:,}) carry no distribution key, "
                "so they are not routed to one shard. Citus's documentation offers this as an approximation of the "
                "cross-shard share: queries on reference and local tables have no key either."
            ),
            evidence={"calls": calls, "without_partition_key": without},
            verify="SELECT executor, partition_key IS NULL AS no_key, sum(calls) FROM citus_stat_statements GROUP BY 1, 2",
        )
    ]


def distribution_columns(columns: list[dict[str, Any]]) -> list[CitusFinding]:
    findings = []
    for column in columns:
        table, name = column["table_name"], column["distribution_column"]
        if column.get("type_name") in _TIME_TYPES:
            findings.append(
                CitusFinding(
                    check="timestamp_distribution_column",
                    severity=Severity.HIGH,
                    confidence=Confidence.PROVEN,
                    summary=(
                        f"{table} is distributed by {name}, a {column['type_name']}. Citus's guidance is not to choose a "
                        "timestamp: new rows all land on the shard for the current time. Distribute by an entity or tenant "
                        "ID and partition by time."
                    ),
                    evidence={"table": table, "column": name, "type": column["type_name"]},
                )
            )
            continue
        distinct = column.get("n_distinct")
        shard_count = int(column.get("shard_count") or 0)
        if distinct is not None and 0 < float(distinct) < shard_count:
            findings.append(
                CitusFinding(
                    check="low_cardinality_distribution_column",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.CANDIDATE,
                    summary=(
                        f"{table} is distributed by {name}, which the planner's sample puts at {float(distinct):.0f} "
                        f"distinct values for {shard_count} shards: at most that many shards can hold rows."
                    ),
                    evidence={"table": table, "column": name, "n_distinct": float(distinct), "shard_count": shard_count},
                    verify=f"SELECT count(DISTINCT {name}) FROM {table}",
                )
            )
    return findings


def keys_without_distribution_column(keys: list[dict[str, Any]]) -> list[CitusFinding]:
    findings = []
    for key in keys:
        columns = list(key.get("columns") or [])
        if key["distribution_column"] in columns:
            continue
        if key["kind"] == "foreign key" and key.get("references_type") != "distributed":
            # A key to a reference or local table needs no distribution column.
            continue
        check = "unique_key_without_distribution_column" if key["kind"] == "unique" else "foreign_key_without_distribution_column"
        findings.append(
            CitusFinding(
                check=check,
                severity=Severity.HIGH,
                confidence=Confidence.PROVEN,
                summary=(
                    f"The {key['kind']} {key['name']} on {key['table_name']} covers ({', '.join(columns)}) without the "
                    f"distribution column {key['distribution_column']}: Citus can enforce it only within a shard."
                ),
                evidence={"table": key["table_name"], "name": key["name"], "columns": columns, "distribution_column": key["distribution_column"]},
            )
        )
    return findings


__all__ = ["assess_citus"]
