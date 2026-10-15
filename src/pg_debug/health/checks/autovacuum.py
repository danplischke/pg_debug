"""Why dead rows are not being cleaned up.

The table bloat check says a table is carrying space its live rows do not need.
That is a symptom, and re-running VACUUM against it is treating the symptom:
bloat that comes back is autovacuum not keeping up, and there are only a few
reasons why it would not.

Either autovacuum is not being asked — the table sits below the threshold that
triggers it, which on a large table is an enormous number of dead rows, or
somebody disabled it — or it is being asked and cannot do anything, because
some transaction, replication slot or prepared transaction is holding the xmin
horizon back. A row deleted after the oldest transaction that might still need
to see it cannot be removed by any amount of vacuuming. That is the case worth
finding, because no amount of tuning autovacuum will fix it.
"""

from __future__ import annotations

from typing import Any

from ...db import SafeSqlDriver
from ...db import SqlDriver
from ..result import HealthCheckResult

AUTOVACUUM_BACKLOG = "Autovacuum backlog"
VACUUM_HORIZON = "Vacuum horizon"

#: Ignore small tables. Autovacuum's default threshold already handles them,
#: and a hundred dead rows is not worth anybody's attention.
DEFAULT_MIN_DEAD_ROWS = 10_000

#: A table this big waits for a fifth of itself to die before the default scale
#: factor triggers a vacuum — 2 million dead rows on a 10 million row table.
#: That is the point at which the default stops being a sensible one.
DEFAULT_LARGE_TABLE_ROWS = 1_000_000

#: A transaction open this long is not a query, it is something someone forgot
#: to commit — and for as long as it lives, nothing deleted after it started
#: can be cleaned up anywhere in the database.
DEFAULT_MIN_TRANSACTION_SECONDS = 900

BACKLOG_SQL = """
WITH defaults AS (
    SELECT
        current_setting('autovacuum_vacuum_threshold')::numeric AS base,
        current_setting('autovacuum_vacuum_scale_factor')::numeric AS scale
),
tables AS (
    SELECT
        s.schemaname AS schema,
        s.relname AS table,
        s.n_dead_tup AS dead_rows,
        s.n_live_tup AS live_rows,
        c.reltuples::numeric AS estimated_rows,
        s.last_autovacuum,
        s.last_vacuum,
        s.autovacuum_count,
        pg_total_relation_size(c.oid) AS table_bytes,
        -- A per-table override wins over the server default, and is exactly
        -- what the fix for a large table looks like.
        (SELECT split_part(opt, '=', 2)::numeric FROM unnest(c.reloptions) opt WHERE opt LIKE 'autovacuum_vacuum_scale_factor=%') AS own_scale,
        (SELECT split_part(opt, '=', 2)::numeric FROM unnest(c.reloptions) opt WHERE opt LIKE 'autovacuum_vacuum_threshold=%') AS own_base,
        coalesce(
            (SELECT split_part(opt, '=', 2) FROM unnest(c.reloptions) opt WHERE opt LIKE 'autovacuum_enabled=%'),
            'true'
        ) NOT IN ('false', 'off', '0') AS autovacuum_enabled
    FROM pg_stat_user_tables s
    JOIN pg_class c ON c.oid = s.relid
    WHERE c.relkind IN ('r', 'm')
),
measured AS (
    SELECT
        t.*,
        coalesce(t.own_base, d.base) + coalesce(t.own_scale, d.scale) * t.estimated_rows AS vacuum_threshold,
        coalesce(t.own_scale, d.scale) AS effective_scale
    FROM tables t
    CROSS JOIN defaults d
)
SELECT
    schema,
    "table",
    dead_rows,
    live_rows,
    estimated_rows::bigint AS estimated_rows,
    table_bytes,
    vacuum_threshold::bigint AS vacuum_threshold,
    effective_scale,
    own_scale IS NOT NULL AS scale_factor_is_set,
    autovacuum_enabled,
    autovacuum_count,
    last_autovacuum,
    last_vacuum,
    CASE
        WHEN NOT autovacuum_enabled THEN 'disabled'
        WHEN dead_rows >= vacuum_threshold THEN 'behind'
        ELSE 'threshold_too_high'
    END AS reason
FROM measured
WHERE dead_rows >= {}
  AND (
      NOT autovacuum_enabled
      OR dead_rows >= vacuum_threshold
      -- A large table on the default scale factor: not behind yet, but the
      -- number of dead rows it will accumulate before autovacuum stirs is the
      -- problem in itself.
      OR (own_scale IS NULL AND estimated_rows >= {})
  )
ORDER BY dead_rows DESC
"""

# Which backends actually hold the horizon is narrower than the usual advice
# about "idle in transaction". Measured on PostgreSQL 16: a READ COMMITTED
# transaction that has only read holds neither an xid nor a snapshot once its
# statement finishes, so it holds back nothing vacuum needs (it still keeps its
# locks, which the connection check judges). One that has written holds an xid;
# one at REPEATABLE READ or above holds a snapshot. Only those two matter.
HORIZON_SQL = """
SELECT
    'transaction' AS kind,
    a.pid::text AS holder,
    coalesce(a.state, 'unknown') AS state,
    CASE WHEN a.backend_xmin IS NOT NULL THEN 'a snapshot' ELSE 'a transaction id' END AS holds,
    age(coalesce(a.backend_xmin, a.backend_xid))::bigint AS transactions_held,
    extract(epoch FROM now() - a.xact_start)::bigint AS seconds_held,
    left(coalesce(a.query, ''), 200) AS detail,
    NULL::text AS application_name,
    NULL::text AS standby
FROM pg_stat_activity a
WHERE (a.backend_xmin IS NOT NULL OR a.backend_xid IS NOT NULL)
  AND a.pid <> pg_backend_pid()
  AND a.xact_start IS NOT NULL
  AND now() - a.xact_start >= make_interval(secs => {})
  -- A WAL sender's backend_xmin is its standby's, reported as the standby below.
  AND a.backend_type IS DISTINCT FROM 'walsender'
UNION ALL
SELECT
    'replication slot',
    s.slot_name,
    CASE WHEN s.active THEN 'active' ELSE 'inactive' END,
    -- A physical slot's xmin is what its standby's hot_standby_feedback
    -- reported, kept while the standby is away.
    CASE WHEN s.slot_type = 'physical' THEN 'its standby''s snapshot, through hot_standby_feedback' ELSE 'a snapshot' END,
    greatest(age(s.xmin), age(s.catalog_xmin))::bigint,
    NULL,
    coalesce(s.plugin, s.slot_type),
    NULL,
    r.application_name
FROM pg_replication_slots s
LEFT JOIN pg_stat_replication r ON r.pid = s.active_pid
WHERE s.xmin IS NOT NULL OR s.catalog_xmin IS NOT NULL
UNION ALL
-- A standby with hot_standby_feedback and no slot: its WAL sender carries
-- the xmin it reports. With a slot, the slot carries it and this is NULL.
SELECT
    'standby',
    coalesce(nullif(r.application_name, ''), r.pid::text),
    coalesce(r.state, 'unknown'),
    'its snapshot, through hot_standby_feedback',
    age(r.backend_xmin)::bigint,
    NULL,
    coalesce(host(r.client_addr), r.client_hostname, ''),
    r.application_name,
    r.application_name
FROM pg_stat_replication r
WHERE r.backend_xmin IS NOT NULL
UNION ALL
SELECT
    'prepared transaction',
    x.gid,
    'prepared',
    'a transaction id',
    age(x.transaction)::bigint,
    extract(epoch FROM now() - x.prepared)::bigint,
    x.database,
    NULL,
    NULL
FROM pg_prepared_xacts x
ORDER BY 5 DESC NULLS LAST
"""


class AutovacuumCalc:
    """Reports why dead rows are accumulating, rather than that they are."""

    def __init__(
        self,
        sql_driver: SqlDriver | SafeSqlDriver,
        min_dead_rows: int = DEFAULT_MIN_DEAD_ROWS,
        large_table_rows: int = DEFAULT_LARGE_TABLE_ROWS,
        min_transaction_seconds: int = DEFAULT_MIN_TRANSACTION_SECONDS,
    ):
        self.sql_driver = sql_driver
        self.min_dead_rows = min_dead_rows
        self.large_table_rows = large_table_rows
        self.min_transaction_seconds = min_transaction_seconds

    async def autovacuum_backlog_check(self) -> HealthCheckResult:
        """Find tables autovacuum is not keeping clean, and say why.

        Three different problems wear the same face. A table can be past the
        threshold and simply waiting its turn; it can have autovacuum switched
        off entirely; or it can be large enough that the default scale factor
        will not trigger until a fifth of it is dead, which is a decision
        nobody made deliberately.
        """
        thresholds = {
            "min_dead_rows": self.min_dead_rows,
            "large_table_rows": self.large_table_rows,
        }

        rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            BACKLOG_SQL,
            [self.min_dead_rows, self.large_table_rows],
        )

        if not rows:
            return HealthCheckResult(
                name=AUTOVACUUM_BACKLOG,
                healthy=True,
                summary="Autovacuum is keeping up.",
                metrics=thresholds,
            )

        findings = [dict(row.cells) for row in rows]
        lines = ["Tables autovacuum is not keeping clean:"]
        for finding in findings:
            lines.append(_describe_backlog(finding))

        return HealthCheckResult(
            name=AUTOVACUUM_BACKLOG,
            healthy=False,
            summary="\n".join(lines),
            findings=findings,
            metrics=thresholds,
        )

    async def vacuum_horizon_check(self) -> HealthCheckResult:
        """Find what is stopping vacuum from removing dead rows at all.

        VACUUM can only remove a row that no transaction might still need to
        see. One old transaction, one replication slot nobody consumes, or one
        prepared transaction left behind pins that horizon for the whole
        database — and until it is gone, no amount of vacuuming reclaims
        anything newer.

        Narrower than the usual "idle in transaction is bad": a READ COMMITTED
        transaction that has only read holds nothing vacuum needs between
        statements and is not reported here; the locks it keeps are the
        connection check's to judge. What is reported is a backend holding a snapshot, or one
        holding a transaction id because it has written.

        A standby with ``hot_standby_feedback`` holds the horizon for the
        queries it runs: through its physical slot's ``xmin`` when it uses a
        slot, through its WAL sender's ``backend_xmin`` in
        ``pg_stat_replication`` when it does not. Either is reported with
        its age. PostgreSQL publishes no age at which a standby's hold
        becomes harmful, so it is judged as a slot is: any hold is reported,
        with how many transactions it holds back.
        """
        thresholds = {"min_transaction_seconds": self.min_transaction_seconds}

        rows = await SafeSqlDriver.execute_param_query(
            self.sql_driver,
            HORIZON_SQL,
            [self.min_transaction_seconds],
        )

        if not rows:
            return HealthCheckResult(
                name=VACUUM_HORIZON,
                healthy=True,
                summary="Nothing is holding back the vacuum horizon.",
                metrics=thresholds,
            )

        findings = [dict(row.cells) for row in rows]
        lines = ["Holding back the vacuum horizon, so dead rows cannot be reclaimed:"]
        for finding in findings:
            lines.append(_describe_horizon(finding))

        return HealthCheckResult(
            name=VACUUM_HORIZON,
            healthy=False,
            summary="\n".join(lines),
            findings=findings,
            metrics=thresholds,
        )


def _describe_backlog(finding: dict[str, Any]) -> str:
    table = f"{finding['schema']}.{finding['table']}"
    dead = int(finding["dead_rows"] or 0)
    threshold = int(finding["vacuum_threshold"] or 0)
    reason = finding["reason"]

    if reason == "disabled":
        return f"Table '{table}' has {dead:,} dead rows and autovacuum switched off for it"
    if reason == "behind":
        last = finding["last_autovacuum"] or finding["last_vacuum"]
        when = f"last vacuumed {last}" if last else "never vacuumed"
        return f"Table '{table}' has {dead:,} dead rows, past its threshold of {threshold:,}, and was {when}"
    scale = float(finding["effective_scale"] or 0)
    return (
        f"Table '{table}' has {int(finding['estimated_rows'] or 0):,} rows and the default scale factor of "
        f"{scale}, so autovacuum waits for {threshold:,} dead rows before it runs"
    )


def _describe_horizon(finding: dict[str, Any]) -> str:
    kind = finding["kind"]
    held = int(finding["transactions_held"] or 0)
    seconds = finding["seconds_held"]
    age = f", holding it back {held:,} transactions" if held else ""

    if kind == "replication slot":
        if finding.get("detail") == "physical":
            if finding.get("standby"):
                whose = f"for standby '{finding['standby']}', whose queries hold the horizon through hot_standby_feedback"
            else:
                whose = "and keeps the xmin its standby last reported through hot_standby_feedback"
            return f"Physical replication slot '{finding['holder']}' is {finding['state']} {whose}{age}"
        return f"Replication slot '{finding['holder']}' is {finding['state']}{age}"
    if kind == "standby":
        address = f" at {finding['detail']}" if finding.get("detail") else ""
        return f"Standby '{finding['holder']}'{address} ({finding['state']}) holds the horizon for its own queries through hot_standby_feedback{age}"
    duration = f" for {int(seconds) // 60} minutes" if seconds else ""
    if kind == "prepared transaction":
        return f"Prepared transaction '{finding['holder']}' has been uncommitted{duration}{age}"
    return f"Backend {finding['holder']} has been {finding['state']}{duration}, holding {finding['holds']}{age}"


__all__ = [
    "AUTOVACUUM_BACKLOG",
    "DEFAULT_LARGE_TABLE_ROWS",
    "DEFAULT_MIN_DEAD_ROWS",
    "DEFAULT_MIN_TRANSACTION_SECONDS",
    "VACUUM_HORIZON",
    "AutovacuumCalc",
]
