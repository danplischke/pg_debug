# Replicas, slots and sharding

What pg-debug can and cannot say about read replicas, replication slots and
logical replication, and how to answer "should we shard?", which usually
means "not yet", with the figure behind it.

## A replica is a separate server

A replica keeps its own statistics. pg-debug run against the primary says
nothing about the queries that run on a replica, and the reverse holds too:
`top-queries`, `health` and `partitions` describe only the node in the
connection URL. When the question is about replica traffic, point the URL at
the replica and run the same commands there:

```bash
pg-debug -d "$REPLICA_URL" --access-mode restricted top-queries --interval 30
pg-debug -d "$REPLICA_URL" --access-mode restricted health --type index
```

Two things follow for advice. An index that `health` reports unused on the
primary may be serving the queries on a replica, so check every replica before
recommending a drop. And a new replica starts its counters from zero, while
most of pg-debug's counter-based findings, the unused-index check among them,
do not yet state how long their counters have been counting; read the window
first (see [platforms.md](platforms.md)) and treat a young replica's counters
as young.

`health --type replication` reports, for the node it is connected to, whether
it is a primary or a replica. On a replica it reads its WAL receiver, to say
whether it is streaming and from which host, and the replay lag in seconds; on
a primary, whether any standby streams from it, leaving out Neon's
`walproposer` connection, which is not one. It lists each replication slot's
name and whether it is active. That is all it reads. It has nothing on
retained WAL, `wal_status`, recovery conflicts or `hot_standby_feedback`; the
sections below read those with `execute`.

What it cannot read it says. A role without `pg_read_all_stats`, which
`pg_monitor` includes, sees the receiver's columns blank, and the check says
it could not tell whether the replica is receiving rather than calling it
stopped. A lag it could not read comes with the reason.

A build from before these readings warns "Replica is not actively replicating
from primary!", with no reason after it, on every replica that has no
standbys of its own, because it counted the replica's own
`pg_stat_replication`, and in restricted mode leaves the lag out without a
word. Before relaying that warning, read the receiver:

```bash
pg-debug -d "$REPLICA_URL" --access-mode restricted execute \
  "SELECT status, sender_host, latest_end_time FROM pg_stat_wal_receiver"
```

`status = streaming` with a recent `latest_end_time` is a working replica.

What a replica's numbers mean depends on the platform:

- **Azure**: Microsoft's documentation contradicts itself on whether Query
  Store records on read replicas. The Query Store page says it does not; the
  newer high-CPU troubleshooting guide says it is supported. Look at the
  replica's `azure_sys` database before saying it has no history.
- **Aurora**: `ReplicaLag` measures how far a replica's page cache trails the
  writer, not WAL replay. Do not compare it with a replay-lag figure.
- **Neon**: a read replica takes `max_connections`,
  `max_prepared_transactions`, `max_locks_per_transaction`, `max_wal_senders`
  and `max_worker_processes` from the primary when it starts, so it has to be
  restarted after the primary is resized. A replica left behind with the old
  values lags.
- **Supabase**: replica lag is in Supabase's own charts, and exclusive locks
  and long transactions on the primary raise it.

## Standby queries against cleanup on the primary

A standby must apply every WAL record the primary wrote, so a standby query
that conflicts with replay is the one that gives way. The conflicts are an
ACCESS EXCLUSIVE lock taken on the primary, a dropped tablespace or database,
vacuum removing rows a standby snapshot can still see, and cleanup on a page a
standby query has pinned. The standby counts them by kind:

```bash
pg-debug -d "$REPLICA_URL" --access-mode restricted execute \
  "SELECT datname, confl_snapshot, confl_lock, confl_bufferpin, confl_deadlock, confl_tablespace FROM pg_stat_database_conflicts"
```

`hot_standby_feedback = on` prevents the conflicts over rows vacuum removed
(`confl_snapshot`), and the PostgreSQL documentation states the price: it
"will delay cleanup of dead rows on the primary, which may result in
undesirable table bloat". Do not recommend it for a standby that runs long
analytics without weighing that bloat. The usual split is a high-availability
standby with low `max_standby_streaming_delay`, perhaps zero, and a dedicated
reporting replica with a high one, hours or -1, that long analytics run on.
`log_recovery_conflict_waits` logs the waits.

Feedback shows up on the primary as bloat that keeps coming back. The Vacuum
horizon check in `health --type vacuum` names the transactions open 15 minutes
or more, the replication slots and the prepared transactions holding the
horizon, but not a standby holding it through `hot_standby_feedback` without a
slot. When bloat returns and that check names nothing, read each standby's
horizon on the primary:

```bash
pg-debug --access-mode restricted execute \
  "SELECT application_name, client_addr, backend_xmin, age(backend_xmin) AS xmin_age FROM pg_stat_replication ORDER BY age(backend_xmin) DESC NULLS LAST"
```

The standby with the largest `xmin_age` holds the horizon furthest back. On
PostgreSQL 16, one REPEATABLE READ transaction on a standby kept 100,000
deleted rows on the primary from being removed while the Vacuum horizon check
reported nothing holding the horizon, and this query named the standby. The
fix is on that standby, by ending or moving its long queries, not more
vacuuming on the primary.

## Replication slots

An inactive slot retains WAL until the disk fills. Since PostgreSQL 13 that is
visible and boundable in `pg_replication_slots`. `wal_status` is `reserved`
(within `max_wal_size`), `extended` (beyond it, kept by a slot or
`wal_keep_size`), `unreserved` (some files go at the next checkpoint) or
`lost` (the slot is unusable). `safe_wal_size` is the bytes that can still be
written before the slot is lost, and is NULL when
`max_slot_wal_keep_size = -1`. PostgreSQL 17 adds `inactive_since` and
`invalidation_reason`; PostgreSQL 18 adds `idle_replication_slot_timeout`,
which invalidates slots left inactive.

pg-debug lists each slot and whether it is active (`health --type
replication`), and names a slot whose `xmin` or `catalog_xmin` holds the
vacuum horizon, with its age (`health --type vacuum`). It reads neither
retained WAL nor `wal_status`. Read them on the primary:

```bash
# Allowed in restricted mode
pg-debug --access-mode restricted execute \
  "SELECT slot_name, slot_type, active, wal_status, safe_wal_size, current_setting('max_slot_wal_keep_size') AS max_slot_wal_keep_size FROM pg_replication_slots"

# Retained bytes need pg_current_wal_lsn() and pg_wal_lsn_diff(), which are not
# on restricted mode's allowlist. It is only a read: say so, and run it
# unrestricted with a timeout.
pg-debug --timeout 10 execute \
  "SELECT slot_name, active, wal_status, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal FROM pg_replication_slots ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC NULLS LAST"
```

Rank the inactive slots by retained WAL and `wal_status`. An inactive slot
with `max_slot_wal_keep_size = -1` is a risk of filling the disk that the
catalog proves: report it with the retained figure, not as a possibility.

**Never advise dropping a slot without confirming its consumer is gone.** A
replica or subscriber whose slot was dropped may have to be rebuilt. Name the
slot, its type and what it retains, and ask who owns it.

## Logical replication

Logical replication copies table rows and not much else. DDL, sequences and
large objects are not replicated, so a schema change has to be applied on both
sides and sequences synchronized before a failover to the subscriber. Views,
materialized views and foreign tables cannot be published.

`pg_stat_replication_slots` (PostgreSQL 14+) counts the transactions that
spilled to disk after outgrowing `logical_decoding_work_mem` (64MB by
default) in `spill_txns`, `spill_count` and `spill_bytes`, and the
documentation names these counters as the input for tuning that setting. The
subscriber's `pg_stat_subscription_stats` (15+) has `apply_error_count` and
`sync_error_count`, and PostgreSQL 18 adds a count per kind of conflict.
pg-debug reads none of these:

```bash
pg-debug --access-mode restricted execute \
  "SELECT slot_name, spill_txns, spill_count, pg_size_pretty(spill_bytes) AS spilled, current_setting('logical_decoding_work_mem') AS logical_decoding_work_mem FROM pg_stat_replication_slots ORDER BY spill_bytes DESC"
```

These are cumulative counters. Take two reads a known time apart before
calling a spill rate high.

## Should we shard? Start from "not yet"

No authoritative numeric threshold for sharding exists. Say so, rather than
repeating rules of thumb such as "CPU above 70%" or "a table over 500GB with
heavy writes", which come from uncited community posts.

The criteria that exist are qualitative. PlanetScale lists a primary
persistently limited by write throughput or disk IOPS, a working set that no
longer fits in one instance's RAM, and a stable routing key that keeps related
data and common queries on one shard, and it puts tuning, schema and query
fixes, faster local storage and larger instances first. Notion sharded because
VACUUM kept stalling and could not reclaim dead rows, not because the data
reached a size.

Do not shard when:

- the load is reads: add replicas;
- a handful of queries take most of the time: tune them (`top-queries`, then
  `diagnose`);
- the problem is retention or bulk deletes: partition (see
  [schema.md](schema.md));
- the data is large but the write rate is healthy;
- no routing key covers most of the workload.

Nor does partitioning stand in for sharding: it "does not remove a
single-primary write bottleneck", and it cannot let the data outgrow one
instance.

### What pg-debug can measure for the decision

| Input | What pg-debug gives today | What it does not show |
| --- | --- | --- |
| Checkpoint pressure | `config` reports when requested checkpoints outnumber timed ones, once at least 10 have happened, with both counts in `evidence` | A manual `CHECKPOINT`, a backup and a shutdown count as requested too; the counts run from the last reset, and pg-debug does not yet say when that was |
| Whether autovacuum keeps up (the Notion symptom) | `health --type vacuum`: Autovacuum backlog, and Vacuum horizon for whatever holds cleanup back | A trend; each run is one moment |
| WAL generation rate | Roughly: the sum of per-query `wal_bytes` over a `top-queries --interval` sample | Only the queries listed, so raise `--limit`; pg-debug does not read `pg_stat_wal`; on PostgreSQL 12 the column reads 0 |
| Working set against memory | Nothing direct: pg-debug does not read `pg_stat_io` and does not guess at RAM | `health --type buffer` gives cache hit rates, but a block served from the OS cache or a platform's cache tier counts as a miss there, so a low rate does not show the working set outgrowing RAM |
| A candidate routing key | `partitions` gives, per large table, the share of its recorded query time that filters on its best column (`pruning_share`) and on each other column it could split by (`evidence.alternatives`) | Each table is weighed on its own; nothing checks that one key also covers the joins between tables |

```bash
pg-debug config
pg-debug health --type vacuum
pg-debug --json top-queries --interval 60 --limit 1000 | jq '[.[].wal_bytes | tonumber] | add'   # bytes in 60s
pg-debug --json partitions --min-size 100MB
```

`top-queries` and `partitions` read `pg_stat_statements` for every database
on the server, not only the one in the URL; on a server with several
databases, say so beside the figures.

Relay a "no" with its numbers. `config` shows the checkpoint counts only when
it raises the finding, so when it is silent read them yourself (on PostgreSQL
17 and later, `num_timed`, `num_requested` and `stats_reset` from
`pg_stat_checkpointer`):

```bash
pg-debug --access-mode restricted execute \
  "SELECT checkpoints_timed, checkpoints_req, stats_reset FROM pg_stat_bgwriter"
```

Checkpoints that come on schedule and autovacuum that keeps up are evidence
against the write-bound primary PlanetScale's first criterion describes,
though not proof of its absence, since neither measures disk IOPS. No column
carrying most of a table's query time is evidence that no routing key covers
the workload. Either is a reason for "not yet". When the numbers point the
other way, they are inputs to a decision the user makes, not a verdict
pg-debug reached.

## Citus

Citus is the mature option. Its guidance:

- **The distribution column** is one of the most important modelling
  decisions. Pick one with high cardinality, because the number of distinct
  values limits the number of shards that can hold the data, and an even
  distribution, because skewed values pile up in a few shards. Multi-tenant
  applications distribute every table by the tenant ID; real-time analytics
  by an entity ID such as user, host or device. **"Do not choose a timestamp
  as the distribution column"**: time series belong in PostgreSQL
  partitioning.
- **Co-location.** A fact table has one distribution key, so co-locate it with
  the dimension it is joined to most and make the other small dimensions
  reference tables. Rows with the same distribution value sit on the same
  machine, which is what makes multi-statement transactions, foreign keys,
  outer joins and `INSERT ... SELECT` work within a co-located set.
- **What does not cross shards.** Correlated subqueries and outer joins
  between distributed tables work only on the distribution column.
  `SELECT ... FOR UPDATE`, `TABLESAMPLE`, recursive CTEs and grouping sets
  work only in single-shard queries. Multi-level partitioned tables cannot be
  distributed, VOLATILE functions are not allowed in UPDATEs, sequences
  smaller than `bigint` can make inserts on the workers fail, and foreign keys
  across distributed schemas are not supported.
- **A poor fit**: a workload that will never outgrow one node, offline
  analytics without real-time ingest or queries, analytics without many
  concurrent users, queries that return ETL-sized results rather than
  summaries, and workloads that move large amounts of data between workers.

pg-debug has no Citus checks. The cluster's own views carry the evidence, and
restricted mode allows reading them:

```bash
pg-debug --access-mode restricted execute "SELECT * FROM citus_stat_tenants"
pg-debug --access-mode restricted execute "SELECT * FROM citus_shards"
```

`citus_stat_tenants` (Citus 11.3) gives each tenant's read count, query count
and CPU seconds per period, needs `citus.stat_tenants_track = 'all'`, uses a
60-second period by default and shows only the top tenants; read its
`_in_last_period` columns. `citus_shards` gives shard sizes, so size skew per
node. In `citus_stat_statements`, the share of calls with no partition key
approximates the share of cross-shard queries.

Azure Cosmos DB for PostgreSQL is on a retirement path: new deployments are
blocked, existing clusters stay supported, and the successor is Azure Database
for PostgreSQL elastic clusters, which are Citus on Flexible Server.

## The newer options, with their maturity

State the maturity with the name, and do not present any of these as a
drop-in:

- **PgDog**: an open-source pooler, load balancer and sharder written in
  Rust, shipping v0.1.x releases weekly. It routes by parsing the SQL for a
  sharding key, or by hints from the application, and added list and range
  sharding in mid-2025. A third-party deployment guide says automated cutover
  supports only one PgDog node and that resharding is still in progress.
- **Multigres**: Supabase's "Vitess for Postgres", announced in June 2025.
  Secondary sources report a v0.1 alpha around mid-2026 that runs as a single
  shard, with horizontal sharding planned for later.
- **Neki**: PlanetScale's sharded Postgres is a preview with no SLA.
- **pgEdge Spock**: multi-master logical replication that resolves conflicts
  by last update wins, by commit timestamp.
- **Postgres-XL**: every source dates from 2014 to 2018. Treat it as dormant.

## Engines that are only PostgreSQL-compatible

CockroachDB, YugabyteDB and Aurora DSQL speak the PostgreSQL protocol without
being PostgreSQL, and that changes what lock and vacuum advice even means.
pg-debug is not tested against any of them. Name the engine before relaying a
lock, vacuum or health finding, and do not relay one that rests on a view the
engine lacks.

- **Aurora DSQL** uses optimistic concurrency with no locks, so conflicts fail
  at commit with SQLSTATE 40001. It caps a transaction at 3,000 modified rows,
  refuses `statement_timeout`, supports no extensions, and has no
  `pg_stat_activity`, `pg_locks` or statistics views. EXPLAIN works there;
  activity, lock and health questions cannot be answered from SQL. Where the
  host or the connection's options name DSQL, pg-debug does not set
  `statement_timeout`; it bounds each statement on its own side and warns
  that this is weaker. That rests on DSQL's documentation and is untested;
  [platforms.md](platforms.md) says what to run there instead.
- **CockroachDB** does not support session-scoped advisory locks:
  `pg_advisory_lock` errors and `pg_try_advisory_lock` always returns true.
- **YugabyteDB** says supporting every PostgreSQL feature in a distributed
  system "is not always feasible"; table inheritance is one it does not
  support.
