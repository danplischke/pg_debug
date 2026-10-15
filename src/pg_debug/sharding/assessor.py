"""Whether the database is ready to shard, which usually means "not yet".

No authoritative numeric threshold for sharding exists. What exists are
reasons not to: the load is reads, which replicas take; a few statements take
most of the time, which tuning fixes; old data is deleted by a time key,
which partitioning turns into dropping a partition; the write path shows no
pressure, so size alone is the argument; or no column carries most of the
workload, so no routing key would keep a query on one shard. Each is argued
here from a figure the server recorded, and any one of them makes the verdict
:attr:`~pg_debug.sharding.result.Verdict.NOT_YET`.

The pressure figures are measured over a short interval, so they describe the
workload of that interval: the WAL written (``pg_stat_wal`` from PostgreSQL 14,
the WAL position before it), shared buffers evicted (``pg_stat_io``, 16), and
checkpoints. Beside them, the cumulative checkpoint counts with their window,
and the autovacuum backlog check. The workload figures, the read share, the
top statements' share and the routing key's, come from ``pg_stat_statements``
over its own window, weighted by execution time, the way ``partitions``
weighs it.

Nothing here guesses at RAM, disk or instance size. Evictions show the working
set does not fit ``shared_buffers``; whether it fits the machine is not
something PostgreSQL records.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from typing import LiteralString
from typing import cast

from pglast import ast
from pglast import parse_sql
from pglast.parser import ParseError

from ..db import SafeSqlDriver
from ..db import SqlDriver
from ..db import get_postgres_version
from ..db import workload_only
from ..db.statements import THIS_DATABASE
from ..db.statements import locate_pg_stat_statements
from ..db.window import checkpoint_window
from ..db.window import statement_window
from ..errors import InvalidArgumentError
from ..errors import PgDebugError
from ..health.checks.autovacuum import AutovacuumCalc
from ..parsing import TableAliasVisitor
from ..partition import PartitionAdvisor
from ..partition import Strategy
from ..partition import extract_predicates
from .citus import assess_citus
from .result import Refusal
from .result import RefusalReason
from .result import ShardReadiness
from .result import Verdict

logger = logging.getLogger(__name__)

#: How long to sample by default. Short, and said so with every figure.
DEFAULT_INTERVAL_SECONDS = 10.0

#: Reads taking this share of the recorded time make it a read load.
READ_SHARE_REFUSAL = 0.8

#: How many statements count as "a few".
TOP_STATEMENTS = 5

#: The top statements taking this share of the time: tune them first.
TOP_SHARE_REFUSAL = 0.5

#: The share of recorded time a routing key has to cover. The same half the
#: partition advisor asks of a partition key: below it, more of the workload
#: fans out to every shard than stays on one.
MIN_ROUTING_SHARE = 0.5

#: Requested checkpoints making up this share of all of them is WAL volume
#: forcing checkpoints, the same reading ``config`` makes.
REQUESTED_CHECKPOINT_SHARE = 0.5

#: Fewer checkpoints than this and the split between timed and requested says
#: nothing.
MIN_CHECKPOINTS = 10

#: How many recorded statements to read.
DEFAULT_WORKLOAD_LIMIT = 500

WAL_STATS_SQL = "SELECT wal_bytes, wal_records, stats_reset, now() AS now FROM pg_stat_wal"
WAL_LSN_SQL = "SELECT pg_current_wal_lsn()::text AS lsn, now() AS now"
RECOVERY_SQL = "SELECT pg_is_in_recovery() AS in_recovery, pg_database_size(current_database()) AS database_bytes"
# Evictions in the normal context are shared buffers given up for another
# block; a bulk read or write uses a ring of its own and is counted apart.
IO_SQL = """
SELECT coalesce(sum(evictions), 0) AS evictions, coalesce(sum(reads), 0) AS reads, coalesce(sum(hits), 0) AS hits
FROM pg_stat_io
WHERE object = 'relation' AND context = 'normal'
"""
CHECKPOINTER_SQL = "SELECT num_timed AS timed, num_requested AS requested FROM pg_stat_checkpointer"
BGWRITER_SQL = "SELECT checkpoints_timed AS timed, checkpoints_req AS requested FROM pg_stat_bgwriter"


def _total_time_sql(total_time: str, relation: str) -> LiteralString:
    return cast(
        LiteralString,
        f"SELECT coalesce(sum({total_time}), 0) AS total_time, count(*) AS statements FROM {relation} WHERE {THIS_DATABASE} AND {workload_only()}",
    )


class ShardReadinessAssessor:
    """Argues "should we shard?" from what the server recorded."""

    def __init__(self, sql_driver: SqlDriver | SafeSqlDriver, workload_limit: int = DEFAULT_WORKLOAD_LIMIT):
        self.sql_driver = sql_driver
        self.workload_limit = workload_limit

    async def assess(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> ShardReadiness:
        """Sample the write path for ``interval_seconds``, read the workload, and say whether to shard.

        Raises:
            InvalidArgumentError: If the interval is not positive.
        """
        if interval_seconds <= 0:
            raise InvalidArgumentError("The sampling interval must be greater than zero seconds.")

        version = await get_postgres_version(self.sql_driver)
        server = (await self._rows(RECOVERY_SQL))[0]
        in_recovery = bool(server.get("in_recovery"))
        notes: list[str] = []
        if in_recovery:
            notes.append(
                "Connected to a standby: its WAL and checkpoints follow the primary's replay, and its workload is its own. "
                "Run this against the primary for the write path."
            )

        before = await self._snapshot(version, in_recovery)
        await asyncio.sleep(interval_seconds)
        after = await self._snapshot(version, in_recovery)

        measurements: dict[str, Any] = {
            "interval_seconds": interval_seconds,
            "database_bytes": int(server.get("database_bytes") or 0),
            "wal": _wal_rate(before, after, version, in_recovery, notes),
            "io": _io_rate(before, after, version, notes),
            "checkpoints": await self._checkpoints(before, after),
            "autovacuum": await self._autovacuum(),
        }

        workload, workload_figures = await self._workload(notes)
        measurements["workload"] = workload_figures
        keys = routing_keys(workload)

        refusals: list[Refusal] = []
        pressure = _pressure(measurements)
        if not workload:
            refusals.append(
                Refusal(
                    reason=RefusalReason.NO_WORKLOAD,
                    summary="There is no recorded workload to judge the load or a routing key by.",
                    instead="Install pg_stat_statements and let it record a representative period of traffic.",
                    changes_if="pg_stat_statements has recorded the workload this database serves.",
                    figures={"statements_read": 0},
                )
            )
        else:
            refusals.extend(_workload_refusals(workload_figures, keys))
            refusals.extend(await self._retention())
        if not pressure:
            refusals.append(_healthy_write_rate(measurements))

        citus = await self._citus(notes)

        if refusals:
            verdict = Verdict.NOT_YET
            summary = f"Not yet: {len(refusals)} reason{'' if len(refusals) == 1 else 's'} not to shard, each with its figure below."
        else:
            verdict = Verdict.WORTH_EVALUATING
            summary = (
                "Worth evaluating: the write path is under pressure, reads do not dominate, no few statements dominate, and "
                f"{keys[0]['column']} covers {keys[0]['share']:.0%} of the recorded time. These are inputs to a decision you "
                "make, not a verdict to shard."
            )

        return ShardReadiness(
            verdict=verdict,
            summary=summary,
            refusals=refusals,
            pressure=pressure,
            would_change=[refusal.changes_if for refusal in refusals],
            routing_keys=keys[:5],
            measurements=measurements,
            citus=citus,
            notes=notes,
        )

    # -- Reading --------------------------------------------------------------

    async def _rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        rows = await SafeSqlDriver.execute_param_query(self.sql_driver, cast(LiteralString, sql), params)
        return [dict(row.cells) for row in rows or []] or [{}]

    async def _snapshot(self, version: int, in_recovery: bool) -> dict[str, Any]:
        """The counters the interval is measured by, read once."""
        snapshot: dict[str, Any] = {}
        if version >= 14:
            snapshot["wal"] = (await self._rows(WAL_STATS_SQL))[0]
        elif not in_recovery:
            # pg_current_wal_lsn() refuses to run during recovery.
            snapshot["lsn"] = (await self._rows(WAL_LSN_SQL))[0]
        if version >= 16:
            snapshot["io"] = (await self._rows(IO_SQL))[0]
        snapshot["checkpoints"] = (await self._rows(CHECKPOINTER_SQL if version >= 17 else BGWRITER_SQL))[0]
        return snapshot

    async def _checkpoints(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        window = await checkpoint_window(self.sql_driver)
        now = after["checkpoints"]
        timed, requested = int(now.get("timed") or 0), int(now.get("requested") or 0)
        total = timed + requested
        return {
            "timed": timed,
            "requested": requested,
            "requested_share": round(requested / total, 3) if total else None,
            "timed_in_interval": timed - int(before["checkpoints"].get("timed") or 0),
            "requested_in_interval": requested - int(before["checkpoints"].get("requested") or 0),
            **window.to_dict(),
        }

    async def _autovacuum(self) -> dict[str, Any]:
        result = await AutovacuumCalc(self.sql_driver).autovacuum_backlog_check()
        reasons: dict[str, int] = {}
        for finding in result.findings:
            reasons[finding["reason"]] = reasons.get(finding["reason"], 0) + 1
        return {
            "keeping_up": not reasons.get("behind") and not reasons.get("disabled"),
            "tables_behind": reasons.get("behind", 0),
            "tables_disabled": reasons.get("disabled", 0),
            "tables_threshold_too_high": reasons.get("threshold_too_high", 0),
            "summary": result.summary,
        }

    async def _workload(self, notes: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The recorded statements, weighted by execution time, and the shares the refusals turn on."""
        stats = await locate_pg_stat_statements(self.sql_driver)
        if stats is None:
            notes.append("pg_stat_statements is not installed in this database, so the workload could not be read.")
            return [], {}
        window = await statement_window(self.sql_driver, stats)
        totals = (await self._rows(_total_time_sql(stats.total_time, stats.relation)))[0]
        workload = await PartitionAdvisor(self.sql_driver).read_workload(self.workload_limit)
        figures = workload_figures(workload, float(totals.get("total_time") or 0.0), int(totals.get("statements") or 0))
        figures.update(window.to_dict())
        return workload, figures

    async def _retention(self) -> list[Refusal]:
        """Tables the partition advisor would partition by time: retention is a partitioning problem."""
        try:
            candidates = await PartitionAdvisor(self.sql_driver).analyze(limit=self.workload_limit)
        except PgDebugError as e:
            logger.info("Could not ask the partition advisor: %s", e)
            return []
        refusals = []
        for candidate in candidates:
            deletes = bool(candidate.evidence.get("deletes_filter_on_this_column"))
            if not candidate.recommended or not (candidate.strategy is Strategy.RANGE or deletes):
                continue
            refusals.append(
                Refusal(
                    reason=RefusalReason.RETENTION,
                    summary=(
                        f"{candidate.qualified_table} ({candidate.table_bytes:,} bytes) is worth partitioning by "
                        f"{candidate.column}: {candidate.pruning_share:.0%} of the recorded time against it filters on it"
                        + (", and its deletes do too." if deletes else ".")
                    ),
                    instead=(
                        f"Partition {candidate.qualified_table} by {candidate.strategy.value} on {candidate.column} "
                        "(pg-debug partitions) and drop old partitions instead of deleting rows."
                    ),
                    changes_if=f"{candidate.qualified_table} is partitioned, or its size is not old data kept by {candidate.column}.",
                    figures={
                        "table": candidate.qualified_table,
                        "table_bytes": candidate.table_bytes,
                        "column": candidate.column,
                        "pruning_share": candidate.pruning_share,
                        "deletes_filter_on_this_column": deletes,
                    },
                )
            )
        return refusals

    async def _citus(self, notes: list[str]) -> dict[str, Any] | None:
        try:
            return await assess_citus(self.sql_driver)
        except PgDebugError as e:
            notes.append(f"Could not read the Citus views: {e}")
            return None


# -- Arithmetic ---------------------------------------------------------------


def _wal_rate(before: dict[str, Any], after: dict[str, Any], version: int, in_recovery: bool, notes: list[str]) -> dict[str, Any]:
    if "wal" in after:
        start, end = before["wal"], after["wal"]
        seconds = _seconds(start.get("now"), end.get("now"))
        written = int(end.get("wal_bytes") or 0) - int(start.get("wal_bytes") or 0)
        return {
            "source": "pg_stat_wal",
            "bytes_in_interval": written,
            "bytes_per_second": round(written / seconds) if seconds else None,
            "bytes_since_reset": int(end.get("wal_bytes") or 0),
            "counting_since": end["stats_reset"].isoformat() if end.get("stats_reset") else None,
        }
    if "lsn" in after:
        seconds = _seconds(before["lsn"].get("now"), after["lsn"].get("now"))
        written = lsn_bytes(after["lsn"].get("lsn")) - lsn_bytes(before["lsn"].get("lsn"))
        return {"source": "pg_current_wal_lsn()", "bytes_in_interval": written, "bytes_per_second": round(written / seconds) if seconds else None}
    notes.append(f"The WAL rate could not be measured: PostgreSQL {version} has no pg_stat_wal, and a standby cannot report its WAL position.")
    return {"source": None}


def _io_rate(before: dict[str, Any], after: dict[str, Any], version: int, notes: list[str]) -> dict[str, Any]:
    if "io" not in after:
        notes.append(f"Buffer evictions could not be measured: pg_stat_io arrived in PostgreSQL 16, and this is {version}.")
        return {"source": None}
    delta = {key: int(after["io"].get(key) or 0) - int(before["io"].get(key) or 0) for key in ("evictions", "reads", "hits")}
    return {"source": "pg_stat_io", **{f"{key}_in_interval": value for key, value in delta.items()}}


def _seconds(start: Any, end: Any) -> float | None:
    try:
        return max((end - start).total_seconds(), 0.0) or None
    except TypeError:
        return None


def lsn_bytes(lsn: Any) -> int:
    """``'16/B374D848'`` as the byte position it names."""
    high, _, low = str(lsn or "0/0").partition("/")
    return (int(high, 16) << 32) + int(low or "0", 16)


def _is_read(stmt: Any) -> bool:
    """A SELECT that locks no rows and writes nothing in a WITH clause."""
    if type(stmt).__name__ != "SelectStmt" or getattr(stmt, "lockingClause", None):
        return False
    with_clause = getattr(stmt, "withClause", None)
    for cte in getattr(with_clause, "ctes", None) or ():
        if type(getattr(cte, "ctequery", None)).__name__ != "SelectStmt":
            return False
    return True


def _user_tables(stmt: Any) -> set[str]:
    visitor = TableAliasVisitor()
    visitor(stmt)
    return {name for name in visitor.tables if not name.startswith(("pg_", "information_schema"))}


def workload_figures(workload: list[dict[str, Any]], total_time: float, statements: int) -> dict[str, Any]:
    """The read share and the top statements' share of the recorded time."""
    read_weight = parsed_weight = 0.0
    for entry in workload:
        try:
            parsed = parse_sql(entry["query"])
        except ParseError:
            continue
        weight = float(entry["weight"])
        parsed_weight += weight
        if parsed and all(_is_read(getattr(raw, "stmt", raw)) for raw in parsed):
            read_weight += weight
    weights = sorted((float(entry["weight"]) for entry in workload), reverse=True)
    denominator = total_time or sum(weights)
    return {
        "statements_recorded": statements,
        "statements_read": len(workload),
        "total_time_ms": round(total_time, 1),
        "time_read_share": round(sum(weights) / denominator, 3) if denominator else None,
        "read_share": round(read_weight / parsed_weight, 3) if parsed_weight else None,
        "top_statements": TOP_STATEMENTS,
        "top_share": round(sum(weights[:TOP_STATEMENTS]) / denominator, 3) if denominator else None,
    }


def routing_keys(workload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Candidate routing keys, by the share of recorded time that filters or joins on them by equality.

    The pruning arithmetic of the partition advisor, across tables: a
    statement counts for a column when, on any user table it reads, it
    compares that column by equality, which is what lets a router send it to
    one shard (or, joining on it, keep the join on co-located shards), and an
    ``INSERT ... VALUES`` counts for each column it names. A statement that
    reads no user table is not counted at all.
    """
    touching = 0.0
    covered: dict[str, float] = {}
    tables: dict[str, set[str]] = {}
    for entry in workload:
        try:
            parsed = parse_sql(entry["query"])
        except ParseError:
            continue
        for raw in parsed:
            stmt = getattr(raw, "stmt", raw)
            if not _user_tables(stmt):
                continue
            weight = float(entry["weight"])
            touching += weight
            predicates = extract_predicates(stmt)
            columns: set[str] = set()
            for table, entries in predicates.by_table.items():
                for column, kind in entries:
                    if kind == "equality":
                        columns.add(column)
                        tables.setdefault(column, set()).add(table)
            for table, column in _inserted_columns(stmt):
                columns.add(column)
                tables.setdefault(column, set()).add(table)
            for column in columns:
                covered[column] = covered.get(column, 0.0) + weight
    if touching <= 0:
        return []
    # Ties go to the column seen on more tables, which co-locates more of them, then by name.
    ranked = sorted(covered.items(), key=lambda item: (-item[1], -len(tables[item[0]]), item[0]))
    return [{"column": column, "share": round(weight / touching, 3), "tables": sorted(tables[column])} for column, weight in ranked]


def _inserted_columns(stmt: Any) -> list[tuple[str, str]]:
    """``(table, column)`` for each column an INSERT ... VALUES names: a router sends the row by its key's value.

    An INSERT ... SELECT is left out: where its rows go depends on what the
    SELECT reads, which the predicates already weigh.
    """
    if not isinstance(stmt, ast.InsertStmt) or not isinstance(stmt.relation, ast.RangeVar):
        return []
    select = stmt.selectStmt
    if not isinstance(select, ast.SelectStmt) or not select.valuesLists:
        return []
    table = str(stmt.relation.relname)
    return [(table, str(target.name)) for target in stmt.cols or () if isinstance(target, ast.ResTarget) and target.name]


def _pressure(measurements: dict[str, Any]) -> list[str]:
    """What presses on the single primary's write path, each with its figure."""
    pressure = []
    checkpoints = measurements["checkpoints"]
    total = checkpoints["timed"] + checkpoints["requested"]
    if total >= MIN_CHECKPOINTS and (checkpoints["requested_share"] or 0) >= REQUESTED_CHECKPOINT_SHARE:
        pressure.append(
            f"{checkpoints['requested']:,} of {total:,} checkpoints were requested rather than timed "
            f"({checkpoints['requested_share']:.0%}): WAL volume forces them, or a manual CHECKPOINT, backup or shutdown did."
        )
    autovacuum = measurements["autovacuum"]
    if autovacuum["tables_behind"]:
        pressure.append(f"Autovacuum is behind on {autovacuum['tables_behind']} table(s): dead rows are past their threshold.")
    return pressure


def _healthy_write_rate(measurements: dict[str, Any]) -> Refusal:
    checkpoints, autovacuum, wal = measurements["checkpoints"], measurements["autovacuum"], measurements["wal"]
    total = checkpoints["timed"] + checkpoints["requested"]
    rate = wal.get("bytes_per_second")
    split = (
        f"{checkpoints['requested']:,} of {total:,} checkpoints requested"
        if total >= MIN_CHECKPOINTS
        else f"only {total} checkpoints recorded, too few to read"
    )
    wal_text = f"{rate:,} bytes of WAL a second over {measurements['interval_seconds']:g} seconds" if rate is not None else "the WAL rate unmeasured"
    return Refusal(
        reason=RefusalReason.HEALTHY_WRITE_RATE,
        summary=(
            f"The write path shows no pressure: {split}, autovacuum keeping up, {wal_text}. "
            f"The database's {measurements['database_bytes']:,} bytes are not on their own a reason to shard."
        ),
        instead="Keep one primary; size alone is answered by partitioning old data, and by a larger instance.",
        changes_if=(
            f"Requested checkpoints reaching {REQUESTED_CHECKPOINT_SHARE:.0%} of at least {MIN_CHECKPOINTS}, or autovacuum "
            "falling behind on tables it cannot catch up with."
        ),
        figures={
            "checkpoints_timed": checkpoints["timed"],
            "checkpoints_requested": checkpoints["requested"],
            "autovacuum_tables_behind": autovacuum["tables_behind"],
            "wal_bytes_per_second": rate,
            "database_bytes": measurements["database_bytes"],
        },
    )


def _workload_refusals(figures: dict[str, Any], keys: list[dict[str, Any]]) -> list[Refusal]:
    refusals = []
    read_share = figures.get("read_share")
    if read_share is not None and read_share >= READ_SHARE_REFUSAL:
        refusals.append(
            Refusal(
                reason=RefusalReason.READ_LOAD,
                summary=f"Reads take {read_share:.0%} of the recorded statement time: the load is reads.",
                instead="Add read replicas and send reads to them; sharding splits writes.",
                changes_if=f"Writes taking more than {1 - READ_SHARE_REFUSAL:.0%} of the recorded time (now {1 - read_share:.0%}).",
                figures={"read_share": read_share},
            )
        )
    top_share = figures.get("top_share")
    if top_share is not None and top_share >= TOP_SHARE_REFUSAL:
        refusals.append(
            Refusal(
                reason=RefusalReason.FEW_QUERIES_DOMINATE,
                summary=f"The top {TOP_STATEMENTS} statements take {top_share:.0%} of the recorded time.",
                instead="Tune them first: pg-debug top-queries, then diagnose each.",
                changes_if=f"The top {TOP_STATEMENTS} statements taking less than {TOP_SHARE_REFUSAL:.0%} of the time (now {top_share:.0%}).",
                figures={"top_share": top_share, "top_statements": TOP_STATEMENTS},
            )
        )
    best = keys[0] if keys else None
    if best is None or best["share"] < MIN_ROUTING_SHARE:
        figure = f"the best, {best['column']}, covers {best['share']:.0%}" if best else "no statement compares any column by equality"
        refusals.append(
            Refusal(
                reason=RefusalReason.NO_ROUTING_KEY,
                summary=f"No routing key covers most of the workload: {figure} of the recorded time.",
                instead="Keep one primary; a sharded database would send most statements to every shard.",
                changes_if=f"One column filtered or joined on by equality in at least {MIN_ROUTING_SHARE:.0%} of the recorded time.",
                figures={"best_key": best["column"] if best else None, "best_share": best["share"] if best else None},
            )
        )
    return refusals


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "MIN_ROUTING_SHARE",
    "READ_SHARE_REFUSAL",
    "TOP_SHARE_REFUSAL",
    "ShardReadinessAssessor",
    "lsn_bytes",
    "routing_keys",
    "workload_figures",
]
