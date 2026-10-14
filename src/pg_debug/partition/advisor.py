"""Whether a table is worth partitioning, decided by what the workload does.

Partitioning is not a general performance feature. It pays when queries filter
on the partition key, so they read one partition instead of all of them, and it
pays most when old rows are removed by that key, because dropping a partition
replaces a DELETE and the vacuum that follows it. It costs when queries *do
not* filter on the key — those now read every partition — and it constrains the
data model, since every unique constraint and primary key has to include the
partition key.

So the number that decides it is not the table's size. It is the share of the
recorded query time that would still prune, and this advisor is built to be
able to say no: a 400 GB table where a third of the query time filters on a
timestamp is a worse table after partitioning, not a better one, and saying so
is more useful than a recommendation.

It stops short of a migration. Converting a populated table means creating the
partitioned one, copying, swapping and reconciling constraints, and generating
that blind is how people lose data — so what comes out is the shape of the
target and the boundaries the data implies, with the conversion named as
somebody else's job.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from typing import LiteralString
from typing import cast

import humanize
from pglast import parse_sql
from pglast.parser import ParseError

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..db import workload_only
from ..db.statements import THIS_DATABASE
from ..db.statements import StatementStatistics
from ..db.statements import locate_pg_stat_statements
from ..parsing import TableAliasVisitor
from .predicates import extract_predicates
from .result import PartitionCandidate
from .result import Strategy

logger = logging.getLogger(__name__)

#: Below this, partitioning costs more in complexity than it returns. Chosen
#: low enough that the retention case — dropping old partitions instead of
#: deleting rows — is still in reach, rather than at the size where pruning
#: alone justifies it.
DEFAULT_MIN_TABLE_BYTES = 1024**3

#: The share of recorded query time that has to keep pruning. Below half,
#: partitioning makes more of the workload slower than it makes faster.
DEFAULT_MIN_PRUNING_SHARE = 0.5

#: More distinct values than this and a LIST partition per value is not
#: something anybody wants to maintain.
MAX_LIST_VALUES = 50

#: Fewer distinct values than this and HASH spreads the table into partitions
#: that are nothing like equal.
MIN_HASH_DISTINCT = 100

#: What a partition should aim to weigh, used only to suggest a HASH modulus.
TARGET_PARTITION_BYTES = 10 * 1024**3

#: How many partitions to write out before summarising the rest. The shape
#: repeats; printing sixty of it teaches nobody anything.
MAX_SHOWN_PARTITIONS = 3

#: A guard on the month walk, for a column whose histogram spans centuries
#: because one row holds a bad date.
MAX_MONTHS = 600

_TIME_TYPES = frozenset({"date", "timestamp", "timestamptz"})
_DISCRIMINATOR_TYPES = frozenset({"int2", "int4", "int8", "text", "varchar", "bpchar", "uuid"})

CANDIDATE_TABLES_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    format('%I.%I', n.nspname, c.relname) AS table_ident,
    pg_total_relation_size(c.oid) AS table_bytes,
    c.reltuples::bigint AS row_count,
    coalesce(s.n_tup_del, 0) AS deletes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind = 'r'
  AND NOT c.relispartition
  AND n.nspname <> 'information_schema'
  AND n.nspname NOT LIKE 'pg\\_%'
  AND pg_total_relation_size(c.oid) >= {}
ORDER BY pg_total_relation_size(c.oid) DESC
"""

COLUMN_STATS_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    a.attname AS column,
    t.typname AS type_name,
    format_type(a.atttypid, a.atttypmod) AS column_type,
    quote_ident(a.attname) AS column_ident,
    s.correlation,
    s.n_distinct,
    CASE WHEN s.n_distinct > 0 THEN s.n_distinct ELSE -s.n_distinct * c.reltuples END::bigint AS distinct_values,
    s.most_common_vals::text::text[] AS common_values,
    s.histogram_bounds::text::text[] AS bounds
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_stats s
    ON s.schemaname = n.nspname AND s.tablename = c.relname AND s.attname = a.attname AND NOT s.inherited
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.oid = ANY({}::regclass[])
"""

# A partitioned table's unique constraints have to include the partition key.
# That is the change partitioning forces on the data model, so the keys that
# would have to change are worth naming before anything else.
KEY_COLUMNS_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS table,
    ic.relname AS index_name,
    i.indisprimary AS is_primary,
    (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest((i.indkey::int2[])[0:i.indnkeyatts - 1]) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum) AS columns
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.indisunique
  AND i.indisvalid
  AND c.oid = ANY({}::regclass[])
"""


# pg_debug's own catalog reads must not decide whether a table is partitioned:
# they filter on nothing the application filters on, and there can be a lot of
# them after a few runs.
def _workload_sql(stats: StatementStatistics) -> str:
    return f"""
SELECT query, calls, {stats.total_time} AS total_exec_time
FROM {stats.relation}
WHERE calls > 0
  AND {THIS_DATABASE}
  AND {workload_only()}
ORDER BY {stats.total_time} DESC
LIMIT {{}}
"""


class PartitionAdvisor:
    """Decides whether a table is worth partitioning, from what the workload does."""

    def __init__(
        self,
        sql_driver: SqlDriver | SafeSqlDriver,
        min_table_bytes: int = DEFAULT_MIN_TABLE_BYTES,
        min_pruning_share: float = DEFAULT_MIN_PRUNING_SHARE,
    ):
        self.sql_driver = sql_driver
        self.min_table_bytes = min_table_bytes
        self.min_pruning_share = min_pruning_share

    async def analyze(self, query_list: list[str] | None = None, limit: int = 200) -> list[PartitionCandidate]:
        """Assess every table large enough to be worth the question.

        Args:
            query_list: Queries to judge by. Defaults to the workload recorded
                in ``pg_stat_statements``, weighted by execution time.
            limit: How many recorded queries to read.

        Returns:
            One :class:`PartitionCandidate` per table examined — including the
            ones it decided against, since "no, and here is the number" is the
            more useful answer more often. Recommended first, then by size.
        """
        tables = await self._candidate_tables()
        if not tables:
            return []

        workload = await self._workload(query_list, limit)
        idents = [table["table_ident"] for table in tables]
        columns = await self._column_stats(idents)
        keys = await self._key_columns(idents)

        candidates = []
        for table in tables:
            candidate = self._assess(table, columns, keys, workload)
            if candidate is not None:
                candidates.append(candidate)

        return sorted(candidates, key=lambda c: (not c.recommended, -c.table_bytes))

    # -- Reading ------------------------------------------------------------

    async def _candidate_tables(self) -> list[dict[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, CANDIDATE_TABLES_SQL, [self.min_table_bytes])
        return [dict(row.cells) for row in rows or []]

    async def _column_stats(self, idents: list[str]) -> dict[str, list[dict[str, Any]]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, COLUMN_STATS_SQL, [idents])
        by_table: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            cells = dict(row.cells)
            by_table.setdefault(f"{cells['schema']}.{cells['table']}", []).append(cells)
        return by_table

    async def _key_columns(self, idents: list[str]) -> dict[str, list[dict[str, Any]]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, KEY_COLUMNS_SQL, [idents])
        by_table: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            cells = dict(row.cells)
            by_table.setdefault(f"{cells['schema']}.{cells['table']}", []).append(cells)
        return by_table

    async def _workload(self, query_list: list[str] | None, limit: int) -> list[dict[str, Any]]:
        """The queries to judge by, with the weight each one carries."""
        if query_list:
            return [{"query": query, "weight": 1.0, "calls": 1} for query in query_list]

        try:
            stats = await locate_pg_stat_statements(self.sql_driver)
            if stats is None:
                return []
            rows = await SafeSqlDriver.execute_param_query(self.sql_driver, cast(LiteralString, _workload_sql(stats)), [limit])
        except Exception as e:
            logger.debug("Could not read the workload: %s", e)
            return []

        workload = []
        for row in rows or []:
            cells = row.cells
            workload.append({"query": cells["query"], "weight": float(cells["total_exec_time"] or 0.0), "calls": int(cells["calls"] or 0)})
        return workload

    # -- Assessment ---------------------------------------------------------

    def _assess(
        self,
        table: dict[str, Any],
        columns: dict[str, list[dict[str, Any]]],
        keys: dict[str, list[dict[str, Any]]],
        workload: list[dict[str, Any]],
    ) -> PartitionCandidate | None:
        name = f"{table['schema']}.{table['table']}"
        table_columns = columns.get(name, [])
        if not table_columns:
            return None

        touching, pruning, deleting = _weigh_workload(workload, table["table"], name)
        if touching <= 0:
            # Nothing recorded touches it, so there is nothing to judge by.
            return None

        best = None
        alternatives = []
        for column in table_columns:
            share = pruning.get(column["column"], {}).get("weight", 0.0) / touching
            strategy = _strategy_for(column, pruning.get(column["column"], {}))
            if strategy is None:
                continue
            # A column nothing measurably filters on is not an alternative to
            # anything; listing it would pad the evidence with noise. Tested
            # against the rounded share so that what is reported as 0% is not
            # also reported as an option.
            if round(share, 3) > 0:
                alternatives.append({"column": column["column"], "strategy": strategy.value, "pruning_share": round(share, 3)})
            # Prefer the larger share; RANGE breaks a tie because it is the one
            # that also makes old data droppable.
            rank = (share, strategy is Strategy.RANGE)
            if best is None or rank > best[0]:
                best = (rank, column, strategy, share)

        if best is None:
            return None

        _rank, column, strategy, share = best
        table_keys = keys.get(name, [])
        blockers = _blockers(table_keys, column["column"])
        recommended = share >= self.min_pruning_share
        reasons = _reasons(table, column, strategy, share, deleting.get(column["column"], 0.0) > 0, recommended)

        return PartitionCandidate(
            schema=table["schema"],
            table=table["table"],
            table_bytes=int(table["table_bytes"] or 0),
            row_count=int(table["row_count"] or 0),
            column=column["column"],
            column_type=column["column_type"],
            strategy=strategy,
            pruning_share=round(share, 3),
            recommended=recommended,
            reasons=reasons,
            blockers=blockers,
            ddl=_ddl(table, column, strategy, table_keys) if recommended else [],
            evidence={
                "query_time_touching_table": round(touching, 1),
                "query_time_that_would_prune": round(pruning.get(column["column"], {}).get("weight", 0.0), 1),
                "correlation": column.get("correlation"),
                "distinct_values": column.get("distinct_values"),
                "recorded_deletes": int(table["deletes"] or 0),
                "deletes_filter_on_this_column": deleting.get(column["column"], 0.0) > 0,
                "alternatives": sorted(alternatives, key=lambda a: -a["pruning_share"]),
            },
        )


# -- Weighing ---------------------------------------------------------------


def _weigh_workload(workload: list[dict[str, Any]], table: str, qualified: str) -> tuple[float, dict[str, dict[str, Any]], dict[str, float]]:
    """Split the recorded time into what touches the table and what prunes.

    Returns ``(touching, pruning, deleting)``: total weight of queries naming
    the table, the weight per column that restricts on it, and the weight of
    deletes doing so — the last being the strongest argument there is.
    """
    touching = 0.0
    pruning: dict[str, dict[str, Any]] = {}
    deleting: dict[str, float] = {}

    for entry in workload:
        try:
            parsed = parse_sql(entry["query"])
        except ParseError as e:
            # One malformed entry among hundreds is not a reason to say nothing
            # about the table.
            logger.debug("Skipping unparseable query (%s): %s...", e, entry["query"][:50])
            continue

        for raw in parsed:
            stmt = getattr(raw, "stmt", raw)
            predicates = extract_predicates(stmt)
            names = [name for name in predicates.by_table if name in (table, qualified)]
            mentions = _mentions_table(stmt, table, qualified)
            if not mentions:
                continue

            weight = float(entry["weight"])
            touching += weight
            for name in names:
                for column, kind in predicates.by_table[name]:
                    entry_for_column = pruning.setdefault(column, {"weight": 0.0, "kinds": set()})
                    entry_for_column["weight"] += weight
                    entry_for_column["kinds"].add(kind)
                    if type(stmt).__name__ == "DeleteStmt":
                        deleting[column] = deleting.get(column, 0.0) + weight

    return touching, pruning, deleting


def _mentions_table(stmt: Any, table: str, qualified: str) -> bool:
    """Whether the statement reads or writes this table.

    Matched against the bare and the qualified name only. A same-named table in
    another schema is a different table, and counting its queries here would
    dilute the share of the one being assessed with time it never spent.
    """
    visitor = TableAliasVisitor()
    visitor(stmt)
    return any(name in (table, qualified) for name in visitor.tables)


def _strategy_for(column: dict[str, Any], pruning: dict[str, Any]) -> Strategy | None:
    """Which partitioning strategy this column could support, if any."""
    kinds = pruning.get("kinds") or set()
    if not kinds:
        return None

    type_name = column.get("type_name")
    distinct = column.get("distinct_values")

    if type_name in _TIME_TYPES:
        return Strategy.RANGE

    if type_name not in _DISCRIMINATOR_TYPES:
        return None

    # A range predicate cannot prune a HASH partition, and LIST needs a small,
    # stable set of values.
    if distinct is not None and 0 < distinct <= MAX_LIST_VALUES:
        return Strategy.LIST
    if kinds == {"equality"} and distinct is not None and distinct >= MIN_HASH_DISTINCT:
        return Strategy.HASH
    return None


def _blockers(keys: list[dict[str, Any]], column: str) -> list[str]:
    """Unique constraints that would have to change to include the key."""
    blockers = []
    for key in keys:
        columns = list(key.get("columns") or [])
        if column in columns:
            continue
        kind = "primary key" if key.get("is_primary") else "unique index"
        blockers.append(
            f"The {kind} {key['index_name']} covers ({', '.join(columns)}) and would have to include {column}: "
            f"a partitioned table cannot enforce uniqueness across partitions without the partition key in the key."
        )
    return blockers


def _reasons(table: dict[str, Any], column: dict[str, Any], strategy: Strategy, share: float, deletes_prune: bool, recommended: bool) -> list[str]:
    """Why the verdict is what it is, in this table's own numbers.

    The share itself is a field of its own, so the first line says what the
    share *means* rather than repeating it: the same 30% is the argument for
    partitioning and the argument against it, depending on which side of the
    threshold it falls, and the direction is the part worth writing down.
    """
    remainder = 1.0 - share
    if recommended:
        reasons = [f"Queries restricting on {column['column']} would read one partition instead of the whole table."]
        if round(remainder, 2) > 0:
            reasons[0] += f" The other {remainder:.0%} of the time would read every partition, which is the cost of doing this."
    else:
        reasons = [
            f"{remainder:.0%} of the recorded time against this table does not restrict on {column['column']}, "
            f"and after partitioning that share reads every partition instead of one table. No other column does "
            f"better. Partitioning would make more of this workload slower than it makes faster."
        ]

    if not recommended:
        # Everything below argues for partitioning. Appending it to a rejection
        # would read as a case for doing the thing just refused.
        return reasons

    if deletes_prune:
        reasons.append(
            f"Deletes in the workload restrict on {column['column']} as well, so those become DROP TABLE on a "
            f"partition — no row-by-row delete, no dead rows to vacuum afterwards. This is usually the larger win."
        )
    elif int(table["deletes"] or 0) > 0:
        reasons.append(
            f"{int(table['deletes']):,} deletes have been recorded on this table. If they remove old rows by "
            f"{column['column']}, partitioning turns them into dropping a partition."
        )

    correlation = column.get("correlation")
    if strategy is Strategy.RANGE and correlation is not None and abs(float(correlation)) > 0.9:
        reasons.append(f"Physical order follows {column['column']} closely (correlation {float(correlation):.2f}), so rows arrive in key order.")

    return reasons


# -- The shape of the target ------------------------------------------------


def _ddl(table: dict[str, Any], column: dict[str, Any], strategy: Strategy, keys: list[dict[str, Any]]) -> list[str]:
    """The shape of the partitioned table, and what the data implies about it.

    Not a migration. Converting a populated table is a separate operation with
    its own risks, and it is named rather than generated.

    Where a unique constraint would have to change, this writes the changed one
    out rather than copying the original: ``LIKE … INCLUDING ALL`` brings the
    old primary key with it, and PostgreSQL then rejects the CREATE outright
    with "unique constraint on partitioned table must include all partitioning
    columns". A sketch that cannot run is worse than no sketch, because the
    error arrives after somebody has already decided to do this.
    """
    ident = table["table_ident"]
    key = column["column_ident"]
    changing = [k for k in keys if column["column"] not in list(k.get("columns") or [])]

    ddl = [
        "-- The target shape. This does NOT convert the existing table: that means creating this,",
        "-- copying the rows, reconciling constraints and swapping the names, which is not something",
        "-- to generate unseen. pg_partman handles the creation and rolling of partitions afterwards.",
    ]
    if changing:
        ddl.append(f"CREATE TABLE {ident}_partitioned (LIKE {ident} INCLUDING ALL EXCLUDING INDEXES) PARTITION BY {strategy.value.upper()} ({key});")
        ddl.append(f"-- The unique constraints have to gain {column['column']}, so they are recreated rather than copied.")
        ddl.append("-- Widening a key changes what the database accepts: rows that collided before may not collide now.")
        for constraint in changing:
            columns = [*(constraint.get("columns") or []), column["column"]]
            kind = "PRIMARY KEY" if constraint.get("is_primary") else "UNIQUE"
            ddl.append(f"ALTER TABLE {ident}_partitioned ADD {kind} ({', '.join(columns)});")
    else:
        ddl.append(f"CREATE TABLE {ident}_partitioned (LIKE {ident} INCLUDING ALL) PARTITION BY {strategy.value.upper()} ({key});")

    if strategy is Strategy.RANGE:
        ddl.extend(_range_partitions(ident, column))
    elif strategy is Strategy.LIST:
        values = sorted(column.get("common_values") or [])
        shown = values[:5]
        for value in shown:
            ddl.append(f"CREATE TABLE {ident}_{_slug(value)} PARTITION OF {ident}_partitioned FOR VALUES IN ('{value}');")
        if len(values) > len(shown):
            ddl.append(f"-- and {len(values) - len(shown)} more; a DEFAULT partition catches anything unlisted.")
    else:
        modulus = _hash_modulus(int(table["table_bytes"] or 0))
        ddl.append(f"-- {modulus} partitions puts each at roughly {humanize.naturalsize(int(table['table_bytes'] or 0) // modulus)}.")
        ddl.append(f"CREATE TABLE {ident}_h0 PARTITION OF {ident}_partitioned FOR VALUES WITH (MODULUS {modulus}, REMAINDER 0);")

    return ddl


def _range_partitions(ident: str, column: dict[str, Any]) -> list[str]:
    """Monthly bounds covering the span the histogram reports.

    The bounds are real rather than placeholders because the span is the one
    thing the catalog can answer and a reader cannot: ``histogram_bounds`` says
    where the data starts and ends. Monthly is the granularity to start from —
    it is the one that matches how retention is usually written — and the
    comment says how to change it rather than pretending it is the only choice.
    """
    bounds = list(column.get("bounds") or [])
    if not bounds:
        return [
            "-- No histogram for this column, so the span is unknown: ANALYZE the table and re-run,",
            "-- or set the boundaries from what you know of the retention window.",
            f"CREATE TABLE {ident}_p0 PARTITION OF {ident}_partitioned FOR VALUES FROM ('start') TO ('end');",
        ]

    first, last = _parse_bound(str(bounds[0])), _parse_bound(str(bounds[-1]))
    if first is None or last is None:
        return [
            f"-- The sampled data runs from {bounds[0]} to {bounds[-1]}; one partition per month is the usual starting point.",
            f"CREATE TABLE {ident}_p0 PARTITION OF {ident}_partitioned FOR VALUES FROM ('start') TO ('end');",
        ]

    months = _months(first, last)
    lines = [
        f"-- The sampled data runs from {bounds[0]} to {bounds[-1]}: {len(months)} monthly partitions cover it.",
        "-- A partition holds one bound inclusive and the other exclusive, so these do not overlap.",
    ]
    for start in months[:MAX_SHOWN_PARTITIONS]:
        end = _next_month(start)
        lines.append(
            f"CREATE TABLE {ident}_{start.strftime('%Y_%m')} PARTITION OF {ident}_partitioned FOR VALUES FROM ('{start.date()}') TO ('{end.date()}');"
        )
    if len(months) > MAX_SHOWN_PARTITIONS:
        lines.append(f"-- and {len(months) - MAX_SHOWN_PARTITIONS} more of the same shape, through {months[-1].strftime('%Y-%m')}.")
    lines.append(f"CREATE TABLE {ident}_default PARTITION OF {ident}_partitioned DEFAULT;  -- catches anything outside the bounds above")
    return lines


def _parse_bound(value: str) -> datetime | None:
    """A histogram bound as a datetime, or None if it is not one we can read."""
    text = value.strip()
    # PostgreSQL renders a timestamptz offset as '+00', which is not ISO 8601
    # until it has minutes on it.
    if re.search(r"[+-]\d{2}$", text):
        text += ":00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _months(first: datetime, last: datetime) -> list[datetime]:
    """The start of every month between the two bounds, inclusive."""
    months = []
    cursor = first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= last and len(months) < MAX_MONTHS:
        months.append(cursor)
        cursor = _next_month(cursor)
    return months or [first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)]


def _next_month(start: datetime) -> datetime:
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


def _hash_modulus(table_bytes: int) -> int:
    """A power of two that puts partitions near the target size."""
    modulus = 4
    while modulus < 256 and table_bytes / modulus > TARGET_PARTITION_BYTES:
        modulus *= 2
    return modulus


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value)[:20].strip("_").lower() or "value"


__all__ = [
    "DEFAULT_MIN_PRUNING_SHARE",
    "DEFAULT_MIN_TABLE_BYTES",
    "MAX_LIST_VALUES",
    "MIN_HASH_DISTINCT",
    "PartitionAdvisor",
]
