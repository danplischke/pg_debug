"""Partitioned tables that already exist, and what their shape costs.

``pg-debug partitions`` answers whether a table is worth partitioning. This
answers the questions a partitioned table raises once it is in use: whether
the next time-range partition exists before rows arrive for it, whether rows
have piled up in the DEFAULT partition (which makes every later ATTACH scan
them, and refuses a partition whose range covers them), whether a unique index
holds only within each partition, whether pruning is switched off, and whether
a query that touches every partition locks more relations than a backend can
lock cheaply.

Every finding but two is read from the catalog. The lock-manager finding is a
candidate: how many partitions a query touches depends on the query, which is
estimated from what ``pg_stat_statements`` records filtering on the partition
key, and stated as the worst case where nothing is recorded. The per-partition
unique index is a candidate in a different sense: that the index exists only
per partition is a catalog fact, that somebody expects it to be global is a
guess.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Any

import humanize

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ...db import get_postgres_version
from ...partition.advisor import read_workload
from ...partition.advisor import weigh_workload
from ...partition.recipes import attach_partition_recipe
from ...severity import Confidence
from ...severity import Severity
from ..result import HealthCheckResult

logger = logging.getLogger(__name__)

PARTITION_HEALTH = "Partition health"

#: Relation locks a backend takes without touching the shared lock table,
#: before PostgreSQL 18 (``FP_LOCK_SLOTS_PER_BACKEND`` in lock.c). Every lock
#: past these goes through the shared table and its partition LWLocks, which
#: is where ``LWLock:LockManager`` waits come from.
FAST_PATH_SLOTS_BEFORE_18 = 16

#: From PostgreSQL 18 the fast-path slots come in groups of this many, and a
#: backend gets enough groups to hold max_locks_per_transaction, rounded up to
#: a power of two, up to :data:`FAST_PATH_MAX_GROUPS`.
FAST_PATH_GROUP_SLOTS = 16
FAST_PATH_MAX_GROUPS = 1024

#: More leaf partitions than this under one parent and the per-query cost of
#: the ones not pruned at plan time (each opened, locked, and given a plan
#: node) is worth stating on its own.
MAX_PARTITIONS = 1000

#: Partitioned partitions this many levels deep and more.
MAX_DEPTH = 3

#: A DEFAULT partition at least this large makes the ATTACH scan of it an
#: outage rather than a pause, the same line ``check-migration`` draws.
LARGE_DEFAULT_BYTES = 1024**3

_TIME_TYPES = frozenset({"date", "timestamp", "timestamptz"})

#: pg_settings.source for a value set with ALTER ROLE or ALTER DATABASE.
_PER_ROLE_SOURCES = frozenset({"database", "user", "database user"})
_LITERAL_RE = re.compile(r"^'((?:[^']|'')*)'(?:::[\w\s]+)?$")
_RANGE_BOUND_RE = re.compile(r"^FOR VALUES FROM \((.*)\) TO \((.*)\)$")

_EXCLUDED_SCHEMAS = "n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'"

# Each root partitioned table, with its tree's size: the relations a query
# that prunes nothing opens and locks, which is every table in the tree and
# every index on each of them (counted on PostgreSQL 16: a SELECT over a
# 12-partition table with two indexes held 42 relation locks), and the most
# any single-partition query locks, which is that partition, its ancestors
# and their indexes.
ROOTS_SQL = f"""
SELECT
    n.nspname AS schema,
    c.relname AS table,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    pt.partstrat AS strategy,
    pg_get_partkeydef(c.oid) AS partition_key,
    (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest(pt.partattrs::int2[]) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum) AS key_columns,
    (SELECT array_agg(quote_ident(a.attname) ORDER BY k.ord)
       FROM unnest(pt.partattrs::int2[]) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum) AS key_idents,
    (SELECT array_agg(t.typname ORDER BY k.ord)
       FROM unnest(pt.partattrs::int2[]) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
       JOIN pg_type t ON t.oid = a.atttypid) AS key_types,
    pt.partnatts AS key_count,
    tree.leaves,
    tree.depth,
    tree.relations,
    (SELECT count(*) FROM pg_partition_tree(c.oid) AS t JOIN pg_index AS i ON i.indrelid = t.relid) AS indexes,
    (SELECT max((SELECT count(*) FROM pg_partition_ancestors(t.relid))
              + (SELECT count(*) FROM pg_partition_ancestors(t.relid) AS a JOIN pg_index AS i ON i.indrelid = a.relid))
       FROM pg_partition_tree(c.oid) AS t WHERE t.isleaf) AS one_partition_locks,
    (SELECT format('%I.%I', dn.nspname, d.relname)
       FROM pg_class AS d JOIN pg_namespace AS dn ON dn.oid = d.relnamespace
      WHERE d.oid = pt.partdefid) AS default_partition
FROM pg_partitioned_table AS pt
JOIN pg_class AS c ON c.oid = pt.partrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN LATERAL (
    SELECT count(*) FILTER (WHERE t.isleaf) AS leaves, max(t.level) AS depth, count(*) AS relations
    FROM pg_partition_tree(c.oid) AS t
) AS tree
WHERE NOT c.relispartition
  AND {_EXCLUDED_SCHEMAS}
ORDER BY n.nspname, c.relname
"""

# Every DEFAULT partition, at any level of a tree, with how many rows it holds
# by both measures the server keeps: the planner's estimate as of the last
# ANALYZE, and the live-tuple counter. The estimate is preferred: the counter
# is approximate (measured on PostgreSQL 16, 500 inserted rows read 1,000
# after an ANALYZE of the parent), but it is all there is for a partition
# never analyzed (reltuples -1 from PostgreSQL 14, 0 before).
DEFAULTS_SQL = f"""
SELECT
    n.nspname AS schema,
    p.relname AS table,
    format('%I.%I', n.nspname, p.relname) AS table_ident,
    (SELECT format('%I.%I', rn.nspname, r.relname)
       FROM pg_class AS r JOIN pg_namespace AS rn ON rn.oid = r.relnamespace
      WHERE r.oid = pg_partition_root(p.oid)) AS root,
    pt.partstrat AS strategy,
    format('%I.%I', dn.nspname, d.relname) AS default_partition,
    d.relkind = 'p' AS default_is_partitioned,
    coalesce(s.n_live_tup, 0) AS live_rows,
    d.reltuples::bigint AS estimated_rows,
    CASE WHEN d.relkind = 'p'
         THEN (SELECT coalesce(sum(pg_relation_size(t.relid)), 0) FROM pg_partition_tree(d.oid) AS t WHERE t.isleaf)
         ELSE pg_relation_size(d.oid) END AS bytes,
    (SELECT array_agg(pg_get_constraintdef(con.oid) ORDER BY con.conname)
       FROM pg_constraint AS con
      WHERE con.conrelid = d.oid AND con.contype = 'c' AND con.convalidated) AS checks
FROM pg_partitioned_table AS pt
JOIN pg_class AS p ON p.oid = pt.partrelid
JOIN pg_namespace AS n ON n.oid = p.relnamespace
JOIN pg_class AS d ON d.oid = pt.partdefid
JOIN pg_namespace AS dn ON dn.oid = d.relnamespace
LEFT JOIN pg_stat_all_tables AS s ON s.relid = d.oid
WHERE pt.partdefid <> 0
  AND {_EXCLUDED_SCHEMAS}
ORDER BY n.nspname, p.relname
"""

# The bounds of a range-partitioned root's own partitions, as PostgreSQL
# renders them, with the server's clock to measure them against.
BOUNDS_SQL = """
SELECT
    format('%I.%I', pn.nspname, p.relname) AS root,
    format('%I.%I', n.nspname, c.relname) AS partition,
    pg_get_expr(c.relpartbound, c.oid) AS bound,
    now() AS now,
    localtimestamp AS local_now
FROM pg_inherits AS i
JOIN pg_class AS c ON c.oid = i.inhrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_class AS p ON p.oid = i.inhparent
JOIN pg_namespace AS pn ON pn.oid = p.relnamespace
WHERE c.relispartition
  AND i.inhparent = ANY({}::regclass[])
"""

# Unique indexes on partitions that belong to no index on their parent: each
# was created on the partition itself, so it holds within that partition only.
UNIQUE_PER_PARTITION_SQL = f"""
SELECT
    n.nspname AS schema,
    r.relname AS table,
    format('%I.%I', n.nspname, r.relname) AS table_ident,
    format('%I.%I', cn.nspname, c.relname) AS partition,
    ic.relname AS index,
    i.indisprimary AS is_primary,
    i.indexprs IS NOT NULL AS has_expressions,
    i.indpred IS NOT NULL AS is_partial,
    (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest((i.indkey::int2[])[0:i.indnkeyatts - 1]) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum = k.attnum) AS columns
FROM pg_partitioned_table AS pt
JOIN pg_class AS r ON r.oid = pt.partrelid
JOIN pg_namespace AS n ON n.oid = r.relnamespace
CROSS JOIN LATERAL pg_partition_tree(r.oid) AS t
JOIN pg_class AS c ON c.oid = t.relid
JOIN pg_namespace AS cn ON cn.oid = c.relnamespace
JOIN pg_index AS i ON i.indrelid = c.oid
JOIN pg_class AS ic ON ic.oid = i.indexrelid
WHERE NOT r.relispartition
  AND t.level > 0
  AND i.indisunique
  AND NOT EXISTS (SELECT 1 FROM pg_inherits AS h WHERE h.inhrelid = i.indexrelid)
  AND {_EXCLUDED_SCHEMAS}
ORDER BY n.nspname, r.relname, cn.nspname, c.relname, ic.relname
"""

# Where enable_partition_pruning is set, other than in this session: per role,
# per database, or both. A role's setting applies at its login, so the session
# pg-debug runs in does not show it.
PRUNING_OVERRIDES_SQL = """
SELECT
    coalesce(r.rolname, '') AS usename,
    coalesce(d.datname, '') AS datname,
    split_part(cfg, '=', 2) AS value
FROM pg_db_role_setting AS s
LEFT JOIN pg_roles AS r ON r.oid = s.setrole
LEFT JOIN pg_database AS d ON d.oid = s.setdatabase
CROSS JOIN LATERAL unnest(s.setconfig) AS cfg
WHERE split_part(cfg, '=', 1) = 'enable_partition_pruning'
"""

SETTINGS_SQL = """
SELECT
    (SELECT setting FROM pg_settings WHERE name = 'enable_partition_pruning') AS enable_partition_pruning,
    (SELECT source FROM pg_settings WHERE name = 'enable_partition_pruning') AS pruning_source,
    current_setting('max_locks_per_transaction')::int AS max_locks_per_transaction
"""

# Relation locks other backends hold right now in the weak modes that are
# eligible for the fast path, split by whether they got a fast-path slot.
# A snapshot, not a rate: it says whether it is happening at this moment.
LOCKS_SQL = """
SELECT
    count(*) FILTER (WHERE fastpath) AS fast_path_locks,
    count(*) FILTER (WHERE NOT fastpath) AS shared_table_locks,
    count(DISTINCT pid) FILTER (WHERE NOT fastpath) AS backends_past_fast_path
FROM pg_locks
WHERE locktype = 'relation'
  AND granted
  AND mode IN ('AccessShareLock', 'RowShareLock', 'RowExclusiveLock')
  AND pid <> pg_backend_pid()
"""

CONTEXT_PRUNING_SQL = "SELECT current_setting('enable_partition_pruning') AS value"


@dataclass
class _Root:
    """One root partitioned table and the facts the findings are read from."""

    schema: str
    table: str
    ident: str
    strategy: str
    partition_key: str
    key_columns: list[str]
    key_idents: list[str]
    key_types: list[str]
    key_count: int
    leaves: int
    depth: int
    relations: int
    indexes: int
    one_partition_locks: int
    default_partition: str | None
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def unpruned_locks(self) -> int:
        """Relation locks a query that prunes nothing takes: every table and index in the tree."""
        return self.relations + self.indexes

    @property
    def time_key(self) -> bool:
        """A range partition on one plain time column, the shape whose bounds run out."""
        return self.strategy == "r" and self.key_count == 1 and len(self.key_types) == 1 and self.key_types[0] in _TIME_TYPES


def fast_path_slots(version: int, max_locks_per_transaction: int) -> int:
    """How many relation locks one backend can take without the shared lock table.

    Sixteen before PostgreSQL 18. From 18 the slots scale with
    max_locks_per_transaction: enough groups of 16 to hold it, the group
    count rounded up to a power of two and capped at 1024.
    """
    if version < 18:
        return FAST_PATH_SLOTS_BEFORE_18
    groups = 1
    while groups < FAST_PATH_MAX_GROUPS and groups * FAST_PATH_GROUP_SLOTS < max_locks_per_transaction:
        groups *= 2
    return groups * FAST_PATH_GROUP_SLOTS


class PartitionHealthCalc:
    def __init__(
        self,
        sql_driver: SqlDriver | SafeSqlDriver,
        max_partitions: int = MAX_PARTITIONS,
        workload_limit: int = 200,
    ):
        self.sql_driver = sql_driver
        self.max_partitions = max_partitions
        self.workload_limit = workload_limit

    async def partition_health_check(self) -> HealthCheckResult:
        """Check the partitioned tables that exist.

        Unhealthy when any partitioned table has a finding; every finding has
        a ``kind``, a ``severity``, a ``confidence``, the ``evidence`` it was
        read from and, where there is one, a ``recipe``.
        """
        roots = await self._roots()
        settings = await self._settings()
        version = await get_postgres_version(self.sql_driver)
        slots = fast_path_slots(version, settings["max_locks_per_transaction"])

        metrics: dict[str, Any] = {
            "partitioned_tables": len(roots),
            "partitions": sum(root.leaves for root in roots),
            "max_partitions": self.max_partitions,
            "fast_path_slots": slots,
            "max_locks_per_transaction": settings["max_locks_per_transaction"],
            "server_version": version,
        }
        if not roots:
            return HealthCheckResult(name=PARTITION_HEALTH, healthy=True, summary="No partitioned tables found.", metrics=metrics)

        findings: list[dict[str, Any]] = []
        findings += await self._pruning_disabled(settings, roots)

        by_name = {root.ident: root for root in roots}
        for root in roots:
            root.findings += self._shape(root)
        for finding in await self._default_rows(by_name):
            by_name[finding.pop("_root")].findings.append(finding)
        for finding in await self._premade_gap([root for root in roots if root.time_key]):
            by_name[finding.pop("_root")].findings.append(finding)
        for finding in await self._unique_per_partition():
            by_name[finding.pop("_root")].findings.append(finding)

        locks, workload_note = await self._lock_pressure(roots, slots)
        for finding in locks:
            by_name[finding.pop("_root")].findings.append(finding)
        metrics["workload"] = workload_note
        metrics["locks_now"] = await self._locks_now()

        for root in roots:
            findings += root.findings

        lines = [f"{len(roots)} partitioned table{'' if len(roots) == 1 else 's'} with {metrics['partitions']:,} partitions examined."]
        if not findings:
            lines.append("No problems found.")
        for finding in findings:
            lines.append(f"[{finding['severity']}] {finding['message']}")
        return HealthCheckResult(
            name=PARTITION_HEALTH,
            healthy=not findings,
            summary="\n".join(lines),
            findings=findings,
            metrics=metrics,
        )

    # -- Reading ------------------------------------------------------------

    async def _roots(self) -> list[_Root]:
        rows = await self.sql_driver.execute_query(ROOTS_SQL)
        roots = []
        for row in rows or []:
            cells = row.cells
            roots.append(
                _Root(
                    schema=cells["schema"],
                    table=cells["table"],
                    ident=cells["table_ident"],
                    strategy=cells["strategy"],
                    partition_key=cells["partition_key"],
                    key_columns=list(cells["key_columns"] or []),
                    key_idents=list(cells["key_idents"] or []),
                    key_types=list(cells["key_types"] or []),
                    key_count=int(cells["key_count"] or 0),
                    leaves=int(cells["leaves"] or 0),
                    depth=int(cells["depth"] or 0),
                    relations=int(cells["relations"] or 0),
                    indexes=int(cells["indexes"] or 0),
                    one_partition_locks=int(cells["one_partition_locks"] or 0),
                    default_partition=cells["default_partition"],
                )
            )
        return roots

    async def _settings(self) -> dict[str, Any]:
        rows = await self.sql_driver.execute_query(SETTINGS_SQL)
        cells = rows[0].cells if rows else {}
        return {
            "enable_partition_pruning": cells.get("enable_partition_pruning"),
            "pruning_source": cells.get("pruning_source"),
            "max_locks_per_transaction": int(cells.get("max_locks_per_transaction") or 64),
        }

    async def _locks_now(self) -> dict[str, int]:
        rows = await self.sql_driver.execute_query(LOCKS_SQL)
        cells = rows[0].cells if rows else {}
        return {key: int(cells.get(key) or 0) for key in ("fast_path_locks", "shared_table_locks", "backends_past_fast_path")}

    # -- Findings -----------------------------------------------------------

    def _shape(self, root: _Root) -> list[dict[str, Any]]:
        """A tree with more partitions, or more levels, than a query plans cheaply."""
        if root.leaves <= self.max_partitions and root.depth < MAX_DEPTH:
            return []
        reasons = []
        if root.leaves > self.max_partitions:
            reasons.append(f"{root.leaves:,} partitions, above {self.max_partitions:,}")
        if root.depth >= MAX_DEPTH:
            reasons.append(f"partitioned {root.depth} levels deep")
        return [
            _finding(
                root,
                "partition_count",
                Severity.LOW,
                Confidence.PROVEN,
                f"{root.qualified} has {' and is '.join(reasons)}. A query that is not pruned at plan time opens, locks "
                f"and plans every one of them: {root.unpruned_locks:,} relation locks with their indexes, against "
                f"{FAST_PATH_SLOTS_BEFORE_18} fast-path slots per backend before PostgreSQL 18. Fewer, larger partitions "
                "cost less per query; each partition should still be prunable to.",
                {"partitions": root.leaves, "depth": root.depth, "relations": root.relations, "indexes": root.indexes},
            )
        ]

    async def _default_rows(self, roots: dict[str, _Root]) -> list[dict[str, Any]]:
        """DEFAULT partitions that hold rows."""
        rows = await self.sql_driver.execute_query(DEFAULTS_SQL)
        findings = []
        for row in rows or []:
            cells = row.cells
            estimated, live = int(cells["estimated_rows"] or 0), int(cells["live_rows"] or 0)
            held = estimated if estimated > 0 else live
            if held <= 0:
                continue
            root = roots.get(cells["root"])
            if root is None:
                continue
            checks = list(cells["checks"] or [])
            size = int(cells["bytes"] or 0)
            default = cells["default_partition"]
            parent = cells["table_ident"]
            message = (
                f"{default}, the DEFAULT partition of {parent}, holds about {held:,} rows ({humanize.naturalsize(size, binary=True)}). "
                f"Every ATTACH PARTITION and CREATE TABLE ... PARTITION OF on {parent} scans it under an ACCESS EXCLUSIVE "
                "lock to prove none of its rows belongs to the new partition, and refuses a partition whose range covers "
                "any of them until they are moved out."
            )
            if checks:
                message += (
                    f" It has {len(checks)} validated CHECK constraint{'' if len(checks) == 1 else 's'}; the scan is skipped "
                    "only when one of them excludes the new partition's range."
                )
            else:
                message += (
                    " It has no validated CHECK constraint, so nothing can let the scan be skipped: add one that excludes "
                    "the new range, NOT VALID and then VALIDATE, before attaching."
                )
            if cells["strategy"] == "r":
                message += " On a range-partitioned table, rows in the DEFAULT usually mean a partition was not created in time."
            evidence = {
                "default_partition": default,
                "live_rows": live,
                "estimated_rows": estimated,
                "bytes": size,
                "validated_checks": checks,
            }
            finding = _finding(
                root,
                "default_partition_rows",
                Severity.MEDIUM if size >= LARGE_DEFAULT_BYTES else Severity.LOW,
                Confidence.PROVEN,
                message,
                evidence,
            )
            if parent != root.ident:
                finding["parent"] = parent
            finding["_root"] = root.ident
            findings.append(finding)
        return findings

    async def _premade_gap(self, roots: list[_Root]) -> list[dict[str, Any]]:
        """Time-range tables whose last partition ends soon, or has ended."""
        if not roots:
            return []
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, BOUNDS_SQL, [[root.ident for root in roots]])
        by_root: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            by_root.setdefault(row.cells["root"], []).append(dict(row.cells))

        findings = []
        for root in roots:
            bounds = []
            now: datetime | None = None
            for cells in by_root.get(root.ident, []):
                parsed = _range_bound(cells["bound"])
                if parsed is None:
                    continue
                lower, upper = parsed
                aware = root.key_types[0] == "timestamptz"
                now = cells["now"] if aware else cells["local_now"]
                lower_at = _point(lower, aware)
                upper_at = _point(upper, aware)
                if upper == "MAXVALUE":
                    bounds = []
                    break
                if upper_at is None:
                    continue
                bounds.append((upper_at, lower_at, upper, cells["partition"]))
            if not bounds or now is None:
                continue

            upper_at, lower_at, upper_text, partition = max(bounds, key=lambda b: b[0])
            ahead = upper_at - now
            width = upper_at - lower_at if lower_at is not None else None
            if width is not None and ahead >= width:
                continue
            if width is None and ahead > timedelta(0):
                # No lower bound to measure a partition's width by, and the
                # last one has not ended: nothing to compare the lead with.
                continue

            ended = ahead <= timedelta(0)
            where = (
                f"they go to the DEFAULT partition {root.default_partition}, which every later ATTACH then has to scan"
                if root.default_partition
                else 'INSERT fails with "no partition of relation found for row"'
            )
            if ended:
                message = (
                    f"{root.qualified}'s last partition, {partition}, ended at {upper_text} ({_span(-ahead)} ago). "
                    f"Rows for later times have no partition: {where}."
                )
            else:
                message = (
                    f"{root.qualified}'s last partition, {partition}, ends at {upper_text}, {_span(ahead)} from now, "
                    f"and no partition exists after it. Rows for later times will have no partition: {where}."
                )
            message += f" If {root.qualified} no longer receives rows for new times, this is expected."
            evidence: dict[str, Any] = {
                "last_partition": partition,
                "upper_bound": upper_text,
                "seconds_ahead": round(ahead.total_seconds()),
                "partition_width_seconds": round(width.total_seconds()) if width is not None else None,
                "default_partition": root.default_partition,
            }
            finding = _finding(root, "premade_partitions", Severity.HIGH if ended else Severity.MEDIUM, Confidence.PROVEN, message, evidence)
            recipe = _next_partition_recipe(root, upper_at, width, partition)
            if recipe:
                finding["recipe"] = recipe
            finding["_root"] = root.ident
            findings.append(finding)
        return findings

    async def _unique_per_partition(self) -> list[dict[str, Any]]:
        """Unique indexes that exist on partitions but on no index of their parent."""
        rows = await self.sql_driver.execute_query(UNIQUE_PER_PARTITION_SQL)
        groups: dict[tuple[str, str, str, tuple[str, ...]], list[dict[str, Any]]] = {}
        for row in rows or []:
            cells = dict(row.cells)
            key = (cells["table_ident"], cells["schema"], cells["table"], tuple(cells["columns"] or ()))
            groups.setdefault(key, []).append(cells)

        findings = []
        for (ident, schema, table, columns), indexes in groups.items():
            listed = ", ".join(columns) if columns else "an expression"
            partitions = sorted({index["partition"] for index in indexes})
            shown = ", ".join(partitions[:3]) + (f" and {len(partitions) - 3} more" if len(partitions) > 3 else "")
            findings.append(
                {
                    "kind": "unique_per_partition",
                    "schema": schema,
                    "table": table,
                    "column": listed,
                    "severity": Severity.MEDIUM.value,
                    "confidence": Confidence.CANDIDATE.value,
                    "message": (
                        f"({listed}) is unique on {len(partitions)} partition{'' if len(partitions) == 1 else 's'} of "
                        f"{schema}.{table} ({shown}) through indexes created on each partition, and on no index of "
                        f"{schema}.{table} itself. Each holds within its own partition only: the same {listed} can be "
                        f"stored once in every partition. If it must be unique across {schema}.{table}, the partition "
                        "key has to be part of the unique index on the parent, or uniqueness enforced in a table "
                        "that is not partitioned."
                    ),
                    "evidence": {
                        "indexes": [
                            {"partition": index["partition"], "index": index["index"], "primary": index["is_primary"], "partial": index["is_partial"]}
                            for index in indexes
                        ],
                        "partitions_with_index": len(partitions),
                    },
                    "_root": ident,
                }
            )
        return findings

    async def _pruning_disabled(self, settings: dict[str, Any], roots: list[_Root]) -> list[dict[str, Any]]:
        """enable_partition_pruning switched off, wherever that was done."""
        findings = []
        consequence = (
            f"With it off the planner cannot drop partitions at plan time and the executor cannot drop them at run time, "
            f"so every query on the {len(roots)} partitioned table{'' if len(roots) == 1 else 's'} here reads every partition."
        )
        # A setting made per role or per database is reported from
        # pg_db_role_setting below, with whom it applies to; this session
        # showing it as well is the same finding.
        if settings["enable_partition_pruning"] == "off" and settings["pruning_source"] not in _PER_ROLE_SOURCES:
            findings.append(
                {
                    "kind": "pruning_disabled",
                    "name": "enable_partition_pruning",
                    "severity": Severity.HIGH.value,
                    "confidence": Confidence.PROVEN.value,
                    "message": f"enable_partition_pruning is off for this session (set by {settings['pruning_source']}). {consequence}",
                    "evidence": {"setting": "off", "source": settings["pruning_source"]},
                }
            )
        rows = await self.sql_driver.execute_query(PRUNING_OVERRIDES_SQL)
        for row in rows or []:
            cells = row.cells
            if str(cells["value"]).strip().strip("'").lower() not in ("off", "false", "0", "no"):
                continue
            scope = " in ".join(
                part
                for part in (f"role {cells['usename']}" if cells["usename"] else "", f"database {cells['datname']}" if cells["datname"] else "")
                if part
            )
            findings.append(
                {
                    "kind": "pruning_disabled",
                    "name": "enable_partition_pruning",
                    "usename": cells["usename"] or None,
                    "datname": cells["datname"] or None,
                    "severity": Severity.HIGH.value,
                    "confidence": Confidence.PROVEN.value,
                    "message": f"enable_partition_pruning is set off for {scope or 'every session'} (ALTER ROLE/DATABASE ... SET). {consequence}",
                    "evidence": {"setting": "off", "source": "pg_db_role_setting"},
                }
            )
        if self.sql_driver.context is not None and settings["enable_partition_pruning"] != "off":
            # The query context's settings are what the application's queries
            # run with, so read the setting there too.
            rows = await self.sql_driver.execute_query(CONTEXT_PRUNING_SQL, in_context=True)
            if rows and rows[0].cells["value"] == "off":
                findings.append(
                    {
                        "kind": "pruning_disabled",
                        "name": "enable_partition_pruning",
                        "severity": Severity.HIGH.value,
                        "confidence": Confidence.PROVEN.value,
                        "message": f"enable_partition_pruning is off in the query context from pg-debug.toml. {consequence}",
                        "evidence": {"setting": "off", "source": "query context"},
                    }
                )
        return findings

    async def _lock_pressure(self, roots: list[_Root], slots: int) -> tuple[list[dict[str, Any]], str]:
        """Partitioned tables a query can lock more relations of than the fast path holds."""
        over = [root for root in roots if root.unpruned_locks > slots]
        if not over:
            return [], "not needed: no partitioned table has more relations than the fast-path slots"
        workload = await read_workload(self.sql_driver, self.workload_limit)
        note = "pg_stat_statements could not be read" if workload is None else f"{len(workload)} recorded statements read"

        findings = []
        for root in over:
            touching = pruning = 0.0
            if workload and root.key_columns:
                touching, by_column, _deletes = weigh_workload(workload, root.table, root.qualified)
                pruning = max((by_column.get(column, {}).get("weight", 0.0) for column in root.key_columns), default=0.0)

            if touching > 0 and pruning >= touching:
                estimate, basis = (
                    root.one_partition_locks,
                    (f"every recorded statement on it filters on {', '.join(root.key_columns)}; assuming each prunes to one partition"),
                )
                severity = Severity.LOW
            elif touching > 0:
                share = 1 - pruning / touching
                estimate, basis = (
                    root.unpruned_locks,
                    (
                        f"{share:.0%} of the recorded time on it is in statements that do not filter on "
                        f"{', '.join(root.key_columns) or 'the partition key'}, and those read every partition"
                    ),
                )
                severity = Severity.MEDIUM
            else:
                estimate = root.unpruned_locks
                basis = "the worst case, a query that prunes nothing: " + (
                    "pg_stat_statements could not be read" if workload is None else "no recorded statement touches it"
                )
                severity = Severity.LOW

            if estimate <= slots:
                continue
            message = (
                f"A query on {root.qualified} can take {estimate:,} relation locks ({basis}), against {slots} fast-path "
                f"slots per backend. Every lock past them goes through the shared lock table, and under concurrency "
                f"that shows as LWLock:LockManager waits. The whole tree is {root.leaves:,} partitions and {root.indexes:,} "
                "indexes; queries that filter on the partition key with a constant, and fewer indexes per partition, keep "
                "a query under the limit."
            )
            evidence = {
                "estimated_locks": estimate,
                "fast_path_slots": slots,
                "unpruned_locks": root.unpruned_locks,
                "one_partition_locks": root.one_partition_locks,
                "basis": basis,
                "recorded_time_ms": round(touching, 1),
                "recorded_time_filtering_on_key_ms": round(pruning, 1),
            }
            finding = _finding(root, "lock_pressure", severity, Confidence.CANDIDATE, message, evidence)
            finding["confirm"] = (
                "Run the query in a transaction, then SELECT count(*) FILTER (WHERE NOT fastpath) FROM pg_locks WHERE pid = "
                "pg_backend_pid() AND locktype = 'relation' before committing: that is how many of its locks missed the fast path."
            )
            finding["_root"] = root.ident
            findings.append(finding)
        return findings, note


# -- Helpers ----------------------------------------------------------------


def _finding(root: _Root, kind: str, severity: Severity, confidence: Confidence, message: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "schema": root.schema,
        "table": root.table,
        "severity": severity.value,
        "confidence": confidence.value,
        "message": message,
        "evidence": evidence,
    }


def _range_bound(text: str | None) -> tuple[str, str] | None:
    """The lower and upper bound of ``FOR VALUES FROM (...) TO (...)``, for a one-column key."""
    match = _RANGE_BOUND_RE.match(text or "")
    if match is None:
        return None
    lower, upper = match.group(1).strip(), match.group(2).strip()
    if "," in _LITERAL_RE.sub("", lower) or "," in _LITERAL_RE.sub("", upper):
        return None
    return lower, upper


def _point(bound: str, aware: bool) -> datetime | None:
    """A rendered bound as a datetime: None for MINVALUE, MAXVALUE, infinity, or anything unreadable."""
    match = _LITERAL_RE.match(bound)
    if match is None:
        return None
    text = match.group(1).replace("''", "'")
    if re.search(r"[+-]\d{2}$", text):
        text += ":00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if aware != (value.tzinfo is not None):
        return None
    return value


def _span(delta: timedelta) -> str:
    """A duration in whole days, or hours under two days: precise enough to schedule by."""
    if delta >= timedelta(days=2):
        return f"{delta.days} days"
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} hour{'' if hours == 1 else 's'}" if hours else "under an hour"


def _next_partition_recipe(root: _Root, start: datetime, width: timedelta | None, last_partition: str) -> list[str]:
    """The lock-aware statements for the partition after the last one."""
    if width is None or width <= timedelta(0) or not root.key_idents:
        return []
    end = _advance(start, width)
    kind = root.key_types[0]
    name = _next_name(last_partition, start)
    return attach_partition_recipe(
        root.ident,
        name,
        root.key_idents[0],
        _literal(start, kind),
        _literal(end, kind),
        default=root.default_partition,
    )


def _advance(start: datetime, width: timedelta) -> datetime:
    """``start`` plus one partition's width, by calendar months when the partitions are months."""
    previous = start - width
    if start.day == previous.day == 1 and start.time() == previous.time() and timedelta(days=28) <= width <= timedelta(days=31 * 12):
        months = (start.year - previous.year) * 12 + start.month - previous.month
        total = start.month - 1 + months
        return start.replace(year=start.year + total // 12, month=total % 12 + 1)
    return start + width


def _literal(value: datetime, kind: str) -> str:
    if kind == "date":
        return f"'{value.date().isoformat()}'"
    return f"'{value.isoformat(sep=' ')}'"


def _next_name(last_partition: str, start: datetime | date) -> str:
    """A name for the next partition, following the last one's if it ends in a date."""
    schema, _, name = last_partition.rpartition(".")
    stem = re.sub(r"(_?p?\d{4}(_?\d{2}){0,2}(_?\d{2,4})?)$", "", name.strip('"'))
    suffix = start.strftime("%Y_%m_%d") if start.day != 1 else start.strftime("%Y_%m")
    candidate = f"{stem or 'partition'}_{suffix}"
    quoted = candidate if re.fullmatch(r"[a-z_][a-z0-9_]*", candidate) else '"' + candidate.replace('"', '""') + '"'
    return f"{schema}.{quoted}" if schema else quoted


__all__ = ["MAX_PARTITIONS", "PARTITION_HEALTH", "PartitionHealthCalc", "fast_path_slots"]
