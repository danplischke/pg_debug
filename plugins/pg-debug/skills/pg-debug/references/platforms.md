# Pooled endpoints and managed platforms

What changes when the database sits behind a transaction pooler or on a
managed or serverless platform: which endpoint to use, which counters to
trust, which settings the platform owns, and what pg-debug cannot see there.

- [pg-debug behind a transaction pooler](#pg-debug-behind-a-transaction-pooler)
- [What a transaction pooler breaks in the application](#what-a-transaction-pooler-breaks-in-the-application)
- [Which endpoint is which](#which-endpoint-is-which)
- [Sizing a pool from evidence](#sizing-a-pool-from-evidence)
- [Identify the platform and the role first](#identify-the-platform-and-the-role-first)
- [Counters start over: state the window](#counters-start-over-state-the-window)
- [Settings the platform fixes or moves](#settings-the-platform-fixes-or-moves), and what pg-debug misreads
- [Evidence outside SQL](#evidence-outside-sql)
- [Three platforms that need care](#three-platforms-that-need-care): Supabase, scale to zero, Aurora DSQL
- [HypoPG on managed platforms](#hypopg-on-managed-platforms)

## pg-debug behind a transaction pooler

Behind a transaction pooler (PgBouncer in transaction mode, Supavisor, Neon's
`-pooler` host, the poolers PlanetScale, Crunchy Bridge, AlloyDB, Cloud SQL
and Azure offer) each transaction may reach a different server backend, and
whatever a session-level command leaves on a backend stays there for the
pooler's next client of the same role and database.

**Check which build you have first.** A version after 0.2.0 has the pooler
fixes. A build that reports 0.2.0 may or may not: the released 0.2.0 does
not, a build from the repository from before the fixes does not, and a build
with them still reports 0.2.0 until the next release. `pg-debug --help` tells
them apart. A build with the fixes describes `--search-path` as the search
path "for every statement"; one without them says "to set on the connection".

A build without the fixes assumes a connection is one backend. Behind a
pooler without prepared-statement support, its `health`, `model`, `tune-*`
and any restricted command that runs six statements or more fail with
`prepared statement "_pg3_0" already exists`. Behind any transaction pooler
it leaves `--timeout`, restricted mode's 30-second default and
`--search-path` set on the pooler's backends, for the next client of the same
role and database to inherit, and it leaves hypothetical indexes behind,
which another client's plain `EXPLAIN` then plans with. Where it has
`index-use` (the released 0.2.0 does not), that command gives a wrong verdict
whenever its statements reach different backends, as they do under load, and
can leave `enable_seqscan` off. With such a build, connect to the direct or
session endpoint in the table below.

A build with the fixes keeps every piece of state inside the transaction that
needs it. It prepares no statement on the server. `--timeout`, restricted
mode's timeout, `--search-path` and the query context are set with
`set_config(..., true)` inside each statement's transaction and end with it.
`index-use` turns sequential scans off inside the forced plan's own
transaction, which is also why restricted mode can take the forced plan.
Hypothetical indexes are created, planned with and reset in one transaction,
and the reset runs even when the plan fails or is cancelled. This is tested through PgBouncer
in transaction mode with prepared statements off and round robin on, which
sends each transaction to the next backend, so such a build can use whichever
endpoint the user gave you. One trace remains: the name of a custom setting.
`--set app.tenant_id=7` leaves `app.tenant_id` defined, as an empty string,
on each backend it ran on.

To tell whether something sits between pg-debug and the server, compare the
port in the URL with the one the server reports:

```bash
pg-debug execute "SELECT inet_server_port()"
```

A different port means something forwards the connection: a pooler or a
proxy, or a plain port mapping such as Docker's or an SSH tunnel, so read it
together with the host. The same port proves nothing: Supavisor's session
mode and Prisma's pooled host both listen on 5432. Over a Unix socket the
function returns NULL. Behind a pooler, `activity` and `locks` show the
pooler's server connections, so `client_addr` is the pooler's address, not
the application's.

## What a transaction pooler breaks in the application

Transaction pooling breaks whatever lives in the session rather than the
transaction: `SET` and `RESET`, SQL-level `PREPARE`, session advisory locks,
`LISTEN`, and `WITH HOLD` cursors. A session `SET` is worse than lost. It
sticks to the server connection and the pooler's next client of the same role
and database inherits it, so the symptom is intermittent and shows only on
the pooled endpoint: `cannot execute UPDATE in a read-only transaction`
(SQLSTATE 25006) after some client set `default_transaction_read_only`, or a
relation that cannot be found after a `SET search_path` reached another
backend.

The fix is to keep the state in the transaction: `SET LOCAL`, or
`set_config(name, value, true)`, inside the transaction that needs it. It
matters most for row-level security, where a tenant set for the session
either vanishes, because the next query reaches a backend without it, or
leaks to whichever client gets that backend next. A custom setting set this
way stays defined afterwards as an empty string, so a policy should read it
as `nullif(current_setting('app.tenant_id', true), '')`, as Supabase's
`auth.uid()` does, rather than test it for NULL.

Prepared statements at the protocol level are the exception a pooler can
carry. PgBouncer supports them from 1.21 when `max_prepared_statements` is
above 0 (200 by default since 1.24); SQL-level `PREPARE` is never covered.
psycopg 3.2 and later use them with PgBouncer 1.22 or later and
`max_prepared_statements` above 0: with libpq 17 as is, with an older libpq
after setting `prepared_max = None` on the connection. Anywhere else pass
`prepare_threshold=None`, because psycopg prepares any statement it has run
five times on a connection, and behind a pooler without support that fails
with `prepared statement "_pg3_0" already exists`. The table below says which
pooled endpoints carry them.

RDS Proxy, in front of RDS and Aurora, does not break a session: it pins the
client to one backend when it sees `SET` (`set_config` included), `PREPARE`, a
temporary table, a cursor, `LISTEN` or a session advisory lock, and stops
multiplexing that client. A call to a stored function does not pin, and the
proxy does not notice what the function changes in the session. Its
`DatabaseConnectionsCurrentlySessionPinned` metric counts pinned sessions.

## Which endpoint is which

| Platform | Direct or session endpoint | Transaction-pooled endpoint | Prepared statements through the pooler |
| --- | --- | --- | --- |
| Neon | host without `-pooler` | host with `-pooler` | yes (`max_prepared_statements` 1000) |
| Supabase | `db.<ref>.supabase.co:5432`, or Supavisor in session mode on 5432 | Supavisor on 6543, or the dedicated PgBouncer on `db.<ref>.supabase.co:6543` | Supavisor: no. Dedicated PgBouncer: Supabase's pages disagree |
| PlanetScale | 5432 | 6432 | yes (200) |
| Crunchy Bridge | 5432 | 5431 | yes (250) |
| AlloyDB, Cloud SQL, Azure | 5432 | 6432, once the opt-in pooler is enabled | no, at the default `max_prepared_statements` of 0 |
| Prisma Postgres | `db.prisma.io` | `pooled.db.prisma.io` | no |

Send work that needs a session to the direct endpoint: `LISTEN`, session
advisory locks, migrations, `pg_dump`, logical replication, long analytics,
and a pg-debug build without the pooler fixes. The opt-in poolers come with
conditions: Cloud SQL offers one only on Enterprise Plus, and enabling it
restarts the instance; Azure's needs `pgbouncer.enabled=true` and is not
offered on the Burstable tier; Crunchy Bridge's refuses superuser and
replication roles, so its `postgres` role connects on 5432.

## Sizing a pool from evidence

What slows a server with too many connections is the connections that exist,
not the setting. In Andres Freund's measurements on PostgreSQL 12, adding
10,000 idle connections cut one active connection from 33,457 to 14,496
transactions per second, the cost being in computing snapshots, while raising
`max_connections` from 100 to 100,000 changed nothing measurable. PostgreSQL
14 made that cost smaller but did not remove it. AWS advises a pooler once
active connections outnumber vCPUs. The "(cores × 2) + spindles" formula
comes from a wiki page last edited in 2014, which says it may need adjusting
from 9.2 onwards and was never analysed on SSDs; do not present it as a
target. No current source found gives a formula in its place, so argue the
size from what the pool reports.

Saturation shows only in the pooler. The server sees the pooler's server
connections, never the clients queued behind them, so neither `activity` nor
`health --type connection` can see a pool that is too small. PgBouncer's
admin console can: `SHOW POOLS` gives `cl_waiting`, the clients that have
sent a query and have no server connection yet, and `maxwait`, how long the
oldest has waited, where a steadily rising `maxwait` means the pool is too
small or the server overloaded; `SHOW STATS` gives `avg_wait_time`.
`pg-debug pooler` reads them, rather than inferring saturation from
server-side counts:

```bash
pg-debug pooler postgresql://admin@pooler:6432/pgbouncer
pg-debug -d postgresql://app@db:5432/app pooler postgresql://admin@pooler:6432/pgbouncer
```

It reports each pool's clients active and waiting, server connections active
and idle, and `maxwait`, with a finding for clients waiting now (high once
the oldest has waited a second), for an average wait over PgBouncer's last
stats period of a millisecond or more, and, with `-d`, for pools that together
can open more server connections than `max_connections` leaves for ordinary
roles. The user must be in `admin_users` or `stats_users`. The admin console
is not PostgreSQL: it is read on its own connection, over the simple query
protocol, and sent only `SHOW` commands, so restricted mode does not apply to
it and nothing in it can change. Give `-d` the server's own address where
you can: through a full pool, reading `max_connections` waits in line, and
the report then says it could not be read. Without `pg-debug pooler` access,
ask the user for `SHOW POOLS` output (`psql` to the pooler's `pgbouncer`
database). On Supabase the Supavisor client count is available only from the
metrics endpoint.

Two settings are worth recommending wherever there is a pool.
`idle_in_transaction_session_timeout` ends sessions left open in a
transaction, which otherwise hold back what vacuum can reclaim; `pg-debug
config` flags it when it is unset. `statement_timeout` belongs on the role
(`ALTER ROLE app_user SET statement_timeout = ...`), not in
`postgresql.conf`, where PostgreSQL's documentation warns it "would affect all
sessions". `config` reads the settings of its own session, so a timeout set on
another role does not show there; this shows what each role sets:

```bash
pg-debug execute "SELECT rolname, rolconfig FROM pg_roles WHERE rolconfig IS NOT NULL"
```

## Identify the platform and the role first

The platform decides which evidence exists, so establish it before relaying a
counter-based finding. The host in the URL usually says: `*.neon.tech`,
`*.supabase.co` or `*.pooler.supabase.com`, `*.horizon.psdb.cloud`
(PlanetScale), `*.db.postgresbridge.com` (Crunchy Bridge), `db.prisma.io`,
`*.tsdb.cloud.timescale.com` (Tiger Cloud). `version()` reads like community
PostgreSQL on Neon and Aurora, and nothing suggests the others differ, so it
does not help. From SQL:

```bash
pg-debug --access-mode restricted execute "SELECT
  current_setting('neon.timeline_id', true) IS NOT NULL AS neon,
  current_setting('supautils.privileged_role', true) IS NOT NULL AS supabase,
  current_setting('rds.extensions', true) IS NOT NULL AS rds_or_aurora,
  to_regproc('aurora_version') IS NOT NULL AS aurora,
  current_setting('azure.extensions', true) IS NOT NULL AS azure,
  (SELECT string_agg(rolname, ', ') FROM pg_roles WHERE rolname IN
    ('neon_superuser', 'rds_superuser', 'pscale_superuser', 'cloudsqlsuperuser',
     'alloydbsuperuser', 'azure_pg_admin', 'tsdbadmin')) AS platform_roles"
```

The role names tell PlanetScale, Cloud SQL, AlloyDB and Tiger Cloud apart.
Aurora DSQL answers `SELECT sys.dsql_major_version()`, run in unrestricted
mode: restricted mode refuses it, because its allowlist does not know the
function. DSQL's hosts are `<cluster>.dsql.<region>.on.aws`, or through
PrivateLink `<cluster>.dsql-<id>.<region>.on.aws`, or a
`vpce-….dsql-<id>.<region>.vpce.amazonaws.com` endpoint with
`-c amzn-cluster-id=<cluster>` in the connection's options. Databricks
Lakebase's hosts are `instance-<id>.database.cloud.databricks.com` or an
`ep-<id>` name under `databricks.com`. These signals come from each platform's
documentation or source code and none has been probed on a live platform, so
read an all-false row as "not recognised", not as "self-hosted".

Connect as a dedicated role holding `pg_monitor`. pg-debug needs no
superuser. `pg_monitor`, which includes `pg_read_all_stats` and
`pg_read_all_settings`, covers the statistics views and settings it reads,
but not the tables: planning a query needs SELECT on them, and `pg_stats`,
which the bloat estimates, `model`, the index advisor and `column-stats`
read, shows only the columns the role can select. A role of its own keeps
pg-debug's sessions apart from the application's on a pooler, and keeps it off
the default admin roles, which bypass row-level security on Supabase
(`postgres`), on Neon projects created after 15 August 2023 and on
PlanetScale, so plans taken as them have no policy predicate. Take plans as
the application's role with `--role`, as
[slow-queries.md](slow-queries.md) describes; the connecting role must be a
member of it.

| Platform | Getting `pg_monitor` |
| --- | --- |
| Neon | roles created in the Console, CLI or API hold it; a role created with SQL does not, and one of the former can grant it |
| Supabase | `postgres` holds it, and on PostgreSQL 16 and later can grant it |
| RDS, Aurora | the master user can grant it, through `rds_superuser` |
| PlanetScale | grantable; PlanetScale documents a monitoring-role recipe |
| Cloud SQL, AlloyDB | `GRANT pg_monitor` works as a `cloudsqlsuperuser` or `alloydbsuperuser` member, though only third parties document it |
| Azure, PostgreSQL 16 and 17 | unproven; try it, and if it is refused grant `pg_read_all_stats` and `pg_read_all_settings` instead |
| Crunchy Bridge | `postgres` is a real superuser |

Without `pg_read_all_stats`, other roles' sessions and query text are hidden
from pg-debug, and only the index advisor, `health --type connection` and
`health --type replication` say so. Check before relaying an
empty or short `activity`, `locks` or `top-queries`:

```bash
pg-debug execute "SELECT pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')"
```

## Counters start over: state the window

Table, index and database counters, and `pg_stat_statements`, count from
their last reset, and on managed platforms resets are routine: every Neon
suspend (after five idle minutes, by default) and compute restart, and on any
platform a crash, a failover, a point-in-time restore or a major upgrade; a
new replica or branch starts with counters of its own.

pg-debug states the window where it argues from counters:
- `health`'s `index`, `scan` and `statistics` checks carry `counting_since`,
  `counting_seconds` and `counting_began_with` (`reset` or `restart`) in
  their metrics and say it in the summary.
- The unused-index check declines to judge until the counters have counted
  for a week (`--min-window-days`, or `min_window_days=` in Python), and
  says so.
- `config`'s spill, checkpoint and cache findings carry the window in their
  evidence.
- Cumulative `top-queries` says on stderr since when its totals count, how
  often entries were evicted, and when a crash lost recent statements.
- `pg.counting_windows()` returns all three windows.

`tune-workload`, `partitions` and the autovacuum checks do not state it yet,
so before relaying them, read it yourself:

```bash
pg-debug execute "SELECT now() - pg_postmaster_start_time() AS uptime,
  (SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()) AS database_reset,
  (SELECT stats_reset FROM pg_stat_bgwriter) AS crash_reset"
pg-debug execute "SELECT stats_reset, dealloc FROM pg_stat_statements_info"
```

On PostgreSQL 17 and later read `crash_reset` from `pg_stat_checkpointer`
instead. Table and index counters count from the later of `database_reset`
and `crash_reset`. After a crash `database_reset` reads NULL while
`crash_reset` moves to the restart, which is why both are read; uptime alone
is no window, because a clean restart moves it and keeps the counters.
`pg_stat_statements_info.stats_reset` (14 and later) is the window of
`top-queries`, `tune-workload` and `partitions`, and it is separate: on a
self-hosted PostgreSQL 16, `pg_stat_statements` survived a crash that reset
everything else, while on Neon it goes with every suspend. A `dealloc` above
zero means entries were evicted to make room. On Supabase the view is
`extensions.pg_stat_statements_info`.

Relay the window with the finding: "scanned 0 times since the compute started
40 minutes ago" is not "unused". No source gives a general minimum window for
calling an index unused (PlanetScale's own advisor waits four weeks, on
history it keeps itself); pg-debug's week is a judgment that covers a weekly
job, not a month-end one. On a platform that resets this often, wait for
representative traffic or say the counters cannot answer yet, rather than
lowering `--min-window-days` to get an answer. On 16 and later
`last_idx_scan` in `pg_stat_user_indexes` says when an index was last used
within the window. Do not suggest resetting statistics to get a clean
baseline: that destroys the only history there is. To rank the workload as it
is now, `top-queries --interval` measures its own window and notices counters
that went backwards.

A reset also empties `last_analyze` and `last_autoanalyze` while the
planner's statistics survive, and two commands misread that. `tune-queries`
and `tune-workload` refuse and ask for `ANALYZE` whenever no table records a
manual `ANALYZE`, because their check reads `last_analyze` alone; and `health
--type statistics` reports every table current, because it cannot judge a
table whose live row count reads 0. `pg-debug column-stats <table>` shows
whether the planner has statistics. PostgreSQL's documentation recommends a
database-wide `ANALYZE` after counters are reset, because autovacuum decides
from them; suggest it rather than running it.

## Settings the platform fixes or moves

A setting the platform owns cannot be changed on advice, and one it re-tunes
may not hold between two readings. Check which kind it is before relaying a
`config` finding:

| Platform | Server settings change through | Fixed or moved by the platform |
| --- | --- | --- |
| Neon | only settings of `context = 'user'`, per session, role or database; instance-level changes only through Support, on the Scale plan | `fsync = off`, `shared_buffers`, `max_wal_size`, `max_connections` and full-page writes are fixed; `max_connections` follows the compute size |
| Supabase | `ALTER ROLE` or `ALTER DATABASE` for user settings; supautils for a list of superuser-only ones; a few such as `shared_buffers` and `max_connections` through `supabase postgres-config update --experimental` | memory settings are tuned to the compute size at boot |
| RDS, Aurora | parameter groups | Aurora serverless sizes `shared_buffers` as it scales and ignores a custom value |
| Cloud SQL, AlloyDB | flags, in the console or API | AlloyDB re-tunes autovacuum at runtime |
| Azure | server parameters, in the portal or CLI | adaptive autovacuum re-tunes five autovacuum settings every 30 minutes; read the effective values with `SHOW` or `config`, not from the control plane |
| Crunchy Bridge | `cb config-param`; `ALTER SYSTEM` works but is lost on failover | |

On AlloyDB, `alloydb.promote_cancel_to_terminate` is on by default: a
statement that hits `statement_timeout` or `lock_timeout` has its backend
terminated rather than cancelled, so a `--timeout`, or restricted mode's 30
seconds, that fires ends the connection.

### What pg-debug misreads on managed platforms

pg-debug recognises the platform from the URL's host and options and from the
server's own settings, functions and roles, and uses it for three things only:
Neon's and Lakebase's durability settings below, the HypoPG advice, and the timeout on Aurora DSQL.
Its other findings do not know the platform, and some are wrong there. Correct
them when relaying.

**Neon's `fsync = off` and `full_page_writes = off`.** Neon fixes both: a
commit is acknowledged once a quorum of safekeepers holds its WAL, so local
fsync is not what makes it durable, and pages are rebuilt from the WAL by the
storage layer, so there is no torn page on the compute's disk for a full-page
image to repair. Where the server has the `neon.timeline_id` setting or the
host is under `neon.tech`, `config` reports each as a low note that names that
signal and does not fail `--exit-code`. Databricks Lakebase, built on Neon,
gets the same notes when the host is a Lakebase endpoint. A `neon_superuser` role alone is not taken as
enough: then it stays HIGH and says why. If the server is Neon all the same,
say so instead of relaying it, and gate with a `--baseline` that records it.
If the low note appears on a server that is not Neon, the recognition was
wrong and `fsync = off` is the risk it is anywhere.

**Neon's `walproposer` row.** Neon's `pg_stat_replication` holds a row with
`application_name = 'walproposer'`, the compute's link to its safekeepers,
which Neon's own checks filter out. `health --type replication` leaves it out
too; a build from before that counted it as a connected replica.

**Connection thresholds.** `health --type connection` judges client backends
against the slots ordinary roles can use (`max_connections` less
`superuser_reserved_connections` and, from 16, `reserved_connections`) and
flags 80%; its `connections_in_use` finding carries each number. Behind a
pooler it counts the pooler's server connections, not its clients. A build
from before flagged 500 connections or 100 idle in transaction whatever
`max_connections` was, while Supabase's recommended `max_connections` runs
from 60 to 500 and Neon's from 104 to 4,000; with such a build, judge the
count yourself:

```bash
pg-debug execute "SELECT count(*) AS client_backends,
  current_setting('max_connections') AS max_connections,
  current_setting('superuser_reserved_connections') AS superuser_reserved,
  current_setting('reserved_connections', true) AS reserved
  FROM pg_stat_activity WHERE backend_type = 'client backend'"
```

`reserved_connections` exists from PostgreSQL 16 and reads NULL before it.

**Cache hit ratios.** `health --type buffer` and `config` count a block found
outside `shared_buffers` as a read. Where a second cache tier serves those
reads (Neon's Local File Cache, Aurora Optimized Reads, AlloyDB's ultra-fast
cache, the Cloud SQL data cache) a low ratio may be a healthy cache. Read the
tier's own counter instead: `neon_stat_file_cache` on Neon, where the target
is 99% or more for OLTP; `orcache_blks_hit` and `storage_blks_read` from
Aurora's `aurora_stat_statements()`; the data cache hit ratio in Cloud SQL's
metrics; on AlloyDB, the `ultra fast cache hit` count that `EXPLAIN` adds to
`Buffers:` once `enable_ultra_fast_cache_explain_output` is on. On Neon
computes of 18 CU and above the file cache is off and the usual ratio
applies.

**Platform traffic in the workload.** `top-queries`, `tune-workload` and
`partitions` read `pg_stat_statements` for every database and every role. On
Supabase that includes the dashboard's queries (tagged `-- source:
dashboard`), Auth, Storage, Realtime and `supabase_admin`'s monitoring; on
Tiger Cloud, the platform's own superuser queries. Before relaying a top query
or tuning for one, check whose it is:

```bash
pg-debug execute "SELECT r.rolname, d.datname, s.calls,
  round(s.total_exec_time) AS total_ms, left(s.query, 60) AS query
  FROM pg_stat_statements s
  JOIN pg_roles r ON r.oid = s.userid JOIN pg_database d ON d.oid = s.dbid
  ORDER BY s.total_exec_time DESC LIMIT 20"
```

On Supabase `pg_stat_statements` lives in schema `extensions`. Qualify it in
a query like this one, and since pg-debug does not schema-qualify it, add
`--search-path public,extensions` if one of its commands cannot find it.

**A failing check aborts `health`.** One check that fails, on a view the
platform lacks or a privilege the role does not hold, takes the whole run
with it. Run the types one at a time with `--type`, and report the ones that
answer and the error of the one that did not.

## Evidence outside SQL

Some of the commonest causes of slowness on a managed platform leave nothing
in the catalog. Name them rather than guessing around them:

| Platform | What to ask about | Where it shows |
| --- | --- | --- |
| Supabase, compute up to 2XL | the disk IO budget: bursts above baseline draw it down, and once it is spent "response times on requests can increase noticeably" and CPU rises with IO wait | "Disk IO % consumed" on the Database Health page; the Metrics API carries the disk IO series but not the budget |
| Azure, Burstable tier | CPU credits: once spent the server runs at its baseline, which Microsoft calls "the most commonly misdiagnosed performance problem" | the `cpu_credits_remaining` and `cpu_credits_consumed` metrics |
| RDS, Aurora | database load by wait event and query over time | CloudWatch Database Insights, which replaced the Performance Insights console |
| Neon | Postgres logs | only through the Datadog or OpenTelemetry integrations (Scale plan, in beta) or Support; Neon has no `auto_explain`, so there is no logged plan for `diagnose --plan` |

Where the platform keeps history that SQL can read, `pg-debug history` reads
it and says how far back each source goes, and, when capture is off, which
setting turns it on. It chooses the sources by the platform it detected, or by
the object being in the database, and reads `pg_qualstats` (the predicates
queries ran with) and `pg_wait_sampling` (what backends waited on) wherever
they are installed. Every read is a catalog lookup first, then a SELECT, so it
works in restricted mode:

```bash
pg-debug --access-mode restricted history
pg-debug --json history | jq '.sources[] | {name, present, capturing, retention, status}'
```

**These readers follow each platform's documentation and have not been run
on the platforms.** Say so when relaying what they return, and fall back to
`execute` with the documented views below if one fails. The sources, and what
the documentation says they keep:

| Platform | What to read | How far back, and when it is there |
| --- | --- | --- |
| Azure | Query Store in database `azure_sys`: `query_store.qs_view`, `query_texts_view`, `pgms_wait_sampling_view`, `query_plans_view` | 7 days by default (up to 30), in 15-minute windows; opt-in, since `pg_qs.query_capture_mode` defaults to `none`; plans only with `pg_qs.store_query_plans` on |
| Aurora | `aurora_stat_statements(true)`: `pg_stat_statements` plus storage and Optimized Reads columns; `aurora_stat_plans(true)`: one row per plan, with its `explain_plan` | the same window as `pg_stat_statements`; plans need `aurora_compute_plan_id` on |
| AlloyDB, Cloud SQL Enterprise Plus | `google_db_advisor_recommended_indexes`, `google_db_advisor_workload_report` | AlloyDB's advisor runs about every 24 hours; Cloud SQL needs `cloudsql.enable_index_advisor`; a non-superuser sees only recommendations from its own queries |
| Neon | `neon_stat_file_cache` | since the compute started; needs `CREATE EXTENSION neon` in the database, which is a write, so ask first; off on computes of 18 CU and above |
| any, with the extension | `pg_qualstats_pretty`, `pg_wait_sampling_profile` | in memory since the extension's reset or the server's start; both need their library in `shared_preload_libraries` |

`history` sets Neon's file cache hit ratio beside the buffer cache hit ratio,
which counts every block the file cache served as a read. It treats the index
advisor's recommendations as the platform's, not pg-debug's: simulate one with
`explain --hypothetical-index` before creating it.

Query Store lives in a database of its own, which any role can read, so point
the URL at it:

```bash
pg-debug -d "postgresql://user@host:5432/azure_sys" --access-mode restricted \
  execute "SELECT * FROM query_store.qs_view LIMIT 20"
```

Microsoft's pages disagree on whether it records on read replicas, so look at
a replica's `azure_sys` before assuming it has nothing. `aurora_stat_statements`
and `aurora_stat_plans` are on restricted mode's list of allowed functions; a
build from before `history` refuses them, and then ask before running them in
unrestricted mode with `--timeout`.

## Three platforms that need care

### Supabase: plan as the application's role

`postgres` bypasses row-level security, so take plans the way PostgREST runs
them, as `authenticated` with the claims a request carries (or `anon` for
requests without a user):

```bash
pg-debug explain --role authenticated \
  --set 'request.jwt.claims={"sub":"<user uuid>","role":"authenticated"}' "<query>"
```

`--role` applies the role's `ALTER ROLE ... SET` values as a login as the
role would, so on Supabase `authenticated` runs with its 8-second
`statement_timeout` (`anon` with 3), unless `--timeout` or restricted mode's
own timeout stands over it, and `--set` stands over any of them. A role
default the connecting user may not set is left out. To see them:
`pg-debug execute "SELECT rolconfig FROM pg_roles WHERE rolname = 'authenticated'"`.

A policy that calls `auth.uid()`, `auth.jwt()` or a security definer function
directly runs it for every row it checks; written as `(select auth.uid())` it
runs once, as an initPlan. In a test, a policy calling `auth.uid()` per row
took 83–104 ms where `(select auth.uid())` took 7 ms, and Supabase's own
example goes from 179 ms to 9 ms. pg-debug has no rule for this yet:
`diagnose` reports the scan's discarded rows with generic advice to index the
filtered columns. When
the scan's `Filter` calls `auth.uid()` itself rather than reading an
`InitPlan` result, recommend the rewrite before any index. Supabase's
Performance Advisor flags the same pattern as `auth_rls_initplan`.

### Scale to zero: Neon and Aurora serverless

Connecting changes what you measure. Any connection wakes a suspended Neon
compute and resets its idle timer, and any connection attempt, even with bad
credentials, resumes a paused Aurora serverless instance, in about 15
seconds, or 30 or more after a day paused. The first command may be slow or
fail to connect while that happens; try again before calling the database
down. After a wake the caches are cold, so the first `diagnose` or `explain
--analyze` timings read pessimistic (Neon measured about 70% lower pgbench
throughput right after a cold restart): take them twice and report the warm
run, or say that the first ran cold. On Neon the counters restarted with the
compute too, so a fresh wake is the worst moment to ask `health` or
`top-queries` anything.

pg-debug's pool keeps no connection while idle. It closes each 30 to 60
seconds after its last use, and all within three minutes, inside Aurora's
shortest five-minute pause delay, so a long-lived client does not keep an
Aurora serverless instance from pausing (Neon documents only that each
connection resets its idle timer). The next call connects again, which wakes
a paused compute. When that reconnect is refused for a reason that will not
clear, such as `max_connections`, pg-debug says so at once with the server's
reason. The CLI exits after each command and is unaffected. An RDS
Proxy in front of Aurora serverless also prevents auto-pause.

### Aurora DSQL is a different engine

Aurora DSQL speaks the PostgreSQL protocol but is not PostgreSQL underneath,
and most of pg-debug's questions do not apply to it. It uses optimistic
concurrency with no locks: a conflict fails at commit (SQLSTATE 40001) rather
than waiting, so there is no blocking to trace. A transaction may modify at
most 3,000 rows. It has no `pg_stat_activity`, `pg_locks`, cumulative
statistics views or `pg_stat_statements`, and no extensions, so no HypoPG
(`pg_stats` is there). It refuses `statement_timeout` at any scope. When the
URL names a DSQL endpoint (its host, a PrivateLink host, or `amzn-cluster-id`
in the connection's options), pg-debug does not set it, and bounds each
statement on its own side instead, with a warning: when that bound passes it
sends PostgreSQL's cancel request, and whether DSQL acts on it has not been
tried, nor whether restricted mode's read-only transactions work there.
`explain` should work in unrestricted mode, but pg-debug's
plan rules do not know DSQL's own nodes (`Storage Scan`, `Storage Lookup`,
`B-Tree Scan`), so read those yourself. Both are read from DSQL's
documentation; pg-debug has not been run against it. `activity`, `locks`,
`health`, `top-queries`, `tune-*` and `explain -i` cannot work there, because
what they read or need is missing; say so rather than trying them one after
another.

## HypoPG on managed platforms

`explain -i`, `tune-queries` and `tune-workload` need HypoPG. Cloud SQL,
Databricks Lakebase, Tiger Cloud, Prisma Postgres and Aurora DSQL do not offer
it. PlanetScale installs it by default, and its own index recommendations
depend on it. Neon, Supabase, RDS, Aurora, AlloyDB, Crunchy Bridge and Azure
offer it for the user to create, Azure after allowlisting.

Where it is offered, installing it is one statement. It changes the user's
database, so ask first, and it is a write, so it needs unrestricted mode:

```bash
pg-debug execute "CREATE EXTENSION hypopg"
```

On Azure, add `hypopg` to the `azure.extensions` server parameter first
(`SHOW azure.extensions` lists what is allowed), and do not create it in
`pg_catalog`: Azure's autonomous tuning drops a copy installed there.

pg-debug's hint follows the platform it recognised, and names the signal it
recognised it by. Where the server lists HypoPG it gives the command above and
asks for the owner's go-ahead; on Azure it puts the allowlist first, with what
`azure.extensions` allows now, and the `pg_catalog` warning, whether or not
the server lists HypoPG yet; on Cloud SQL, Databricks Lakebase, Tiger Cloud,
Prisma Postgres and Aurora DSQL it says the platform does not offer HypoPG and
what still works. On a server it does not recognise it still gives package
instructions, which apply only to a server the user administers; if the
platform is managed after all, ignore them and check its list of supported
extensions.

Where HypoPG is not offered, pg-debug cannot simulate an index. `explain`,
`diagnose` and `index-use` still work, and the plan is the evidence there is.
`tune-queries` returns candidates inferred from `pg_stats` instead, marked as
never costed by the planner, and on Cloud SQL and AlloyDB adds
`google_db_advisor_recommend_indexes()` where that extension is installed
(unrestricted mode only; not tried against either platform).
On Cloud SQL Enterprise Plus the platform's own index advisor, above, is the
nearest substitute; elsewhere, testing an index means building it on a branch
or a copy of the database and measuring.
