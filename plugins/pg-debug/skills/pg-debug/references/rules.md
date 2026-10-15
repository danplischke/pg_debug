# Rules

Every kind of finding `health`, `model`, `config` and `logs` report has a rule
id: lower case, stable across releases, unique across the commands. In
`--json` each finding carries its `rule` and a `docs` link to its entry here
(a health check also lists its `rules`). Suppress a rule by its id, and look
it up here by the same id.

Each entry says what the rule reads, which decides where it can answer:

| Reads | What | Where it can run |
| --- | --- | --- |
| `catalog` | The schema's definitions: tables, columns, constraints, indexes | Anywhere, including a freshly migrated, empty CI database. `--static` runs only these. |
| `data` | What the tables hold: sizes, row estimates, ANALYZE's samples | A database with production-like data. Finds nothing on an empty one. |
| `workload` | Counters and sessions the running workload leaves | Only the database the workload runs on. |
| `settings` | The server's settings | The server whose settings they are; a CI database seldom shares them. |
| `log` | A server log handed over | No connection at all. |

## Gating a build

```bash
pg-debug model --static --exit-code                     # CI, on a freshly migrated database
pg-debug health --static --exit-code --baseline b.json   # the static health checks, against accepted findings
pg-debug model --exit-code --suppress naive_timestamp    # a rule the team has decided against
pg-debug plans --queries critical.sql --baseline plans.json --exit-code   # fail when a plan's shape changes
```

**Suppressing a rule.** A rule switched off for good goes in `pg-debug.toml`,
with the reason, which every run prints:

```toml
[suppress]
naive_timestamp = "Every timestamp is UTC by convention; the application never stores local time."
random_page_cost_default = "Measured on our storage: 4.0 is right here."
```

`--suppress RULE` (repeatable) does the same for one run. A suppressed
finding is still reported: in JSON it carries `"suppressed": "<reason>"`, and
the text ends with how many each rule held back. It never fails
`--exit-code`. An id that is not a rule is an error, so a misspelling does
not silently suppress nothing. `health`, `model` and `config` read the
`[suppress]` table from the `pg-debug.toml` in the working directory or the
nearest parent; `--config FILE` names another, `--no-config` reads none.

**`--static`** on `health` and `model` runs only the rules that read the
catalog alone: invalid and duplicate indexes and invalid constraints, and the
key, type and column rules of the data model review. The rest would pass an
empty database for want of anything to see, and that is not a pass, so they
do not run rather than run and say nothing. `config` has no static rules: its
findings read settings or counters, and a CI server's are not production's.
A static rule's severity can still weigh a table's size, which an empty table
makes low: on a CI database, `unindexed_foreign_key` is found, but low.

**Baselines** (`--baseline`, `--save-baseline`) hold what each finding is
about. Since format version 2 an entry also holds a `fingerprint` of the
definition of what it names: `pg_get_indexdef` for an index (with the index
covering it, for a duplicate), `pg_get_constraintdef` for a constraint, each
column's type, `NOT NULL`, collation and default for findings about columns,
and the table's columns for a data model finding about a whole table. A
finding whose object is defined differently than when it was accepted is
`changed`, not `known`, and fails the gate as a new one would. Findings about
a table's state (bloat, dead rows, scans), sessions and settings carry no
fingerprint. Version 1 files still load; their findings are known or new.

**Plan shapes.** `pg-debug plans --queries FILE` plans each query with
`EXPLAIN` (no `ANALYZE`, nothing runs) under the query context, and records
its shape: node types, join, aggregate and scan strategies, and the relations,
functions, CTEs and indexes each node reads, without costs or row counts. A
`-- name: ...` line above a query names it; otherwise it is known by its
fingerprint, which ignores constants, so two queries that differ only in a
constant need names. Against `--baseline`, a query whose shape differs is
`changed` and, with `--exit-code`, exits 3; the text shows the accepted and
the current shape as a diff. A query the baseline does not hold is `new` and
does not fail. A query that cannot be planned makes the exit status 1, and
`--save-baseline` saves nothing from such a run. The planner decides from the
database it is asked on: compare a CI run against a baseline taken in CI, from
the same seed data.

## health

### invalid_index

Reads: catalog. Reported by the "Invalid index check". An index whose
`CREATE INDEX CONCURRENTLY` or `REINDEX CONCURRENTLY` failed: it is
maintained on every write and used by no query. Drop it and build it again.

### duplicate_index

Reads: catalog. Reported by the "Duplicate index check". An index whose
columns another valid index of the same method already leads with, and which
enforces nothing itself. It costs writes and space for nothing; drop it after
checking no constraint depends on it.

### index_bloat

Reads: data. Reported by "Index bloat". An index much larger than its rows
need, estimated from the catalog's sizes and statistics. `REINDEX
CONCURRENTLY` rebuilds it.

### unused_index

Reads: workload. Reported by the "Unused index check". An index no scan has
used while the counters counted, judged only once they have counted
`--min-window-days`. Check replicas and rare jobs before dropping it.

### connections_in_use

Reads: workload. Reported by "Connection health" with `kind`
`connections_in_use`: client connections at 80% or more of the slots ordinary
roles can use.

### idle_in_transaction

Reads: workload. Reported by "Connection health" with `kind`
`idle_in_transaction`: a session idle in a transaction for a minute or more,
holding its locks and its connection. Known in a baseline by its role,
database and application name.

### transaction_id_wraparound

Reads: workload. Reported by "Vacuum health": a table approaching
transaction id wraparound, which PostgreSQL stops accepting writes to avoid.

### autovacuum_backlog

Reads: workload. Reported by "Autovacuum backlog": a table past its
autovacuum threshold, with autovacuum off for it, or large enough that the
default scale factor waits too long.

### vacuum_horizon

Reads: workload. Reported by "Vacuum horizon": a transaction, replication
slot or prepared transaction stopping vacuum from removing dead rows at all.

### sequence_exhaustion

Reads: data. Reported by "Sequence health": a sequence, or the column it
feeds, approaching its maximum.

### replica_not_receiving

Reads: workload. Reported by "Replication health": a replica whose WAL
receiver is not streaming. The check's findings list the replication slots
whatever its verdict, so they carry no rule of their own.

### index_cache_hit_rate

Reads: workload. Reported by "Buffer health for indexes": index reads
missing the buffer cache.

### table_cache_hit_rate

Reads: workload. Reported by "Buffer health for tables": table reads missing
the buffer cache.

### invalid_constraint

Reads: catalog. Reported by "Constraint health": a constraint added `NOT
VALID` and never validated, so existing rows were never checked against it.

### stale_statistics

Reads: workload. Reported by "Statistics health": a table never analyzed, or
changed a great deal since.

### table_bloat

Reads: data. Reported by "Table bloat": a table wasting at least 100 MB and
20% of its size, estimated from `pg_stats` and `pg_class`.

### sequential_scans

Reads: workload. Reported by "Sequential scan health": a table read
sequentially far more than through an index, charged in bytes read.

## model

### unindexed_foreign_key

Reads: catalog. A foreign key with no index behind it: every delete or key
update on the referenced table scans the referencing one. Severity weighs the
table's size and the deletes recorded against its parent.

### foreign_key_type_mismatch

Reads: catalog. A foreign key whose two sides differ in type, typmod or
collation.

### missing_primary_key

Reads: catalog. A table without a primary key; high when nothing else
identifies a row either.

### column_order_padding

Reads: data. Columns ordered so that alignment padding costs bytes on every
row; the saving is measured from the table's rows.

### naive_timestamp

Reads: catalog. A `timestamp without time zone` column.

### blank_padded_char

Reads: catalog. A `char(n)` column.

### nullable_never_null

Reads: data. A nullable column ANALYZE's sample never saw NULL in. A
candidate, with the query that confirms it.

### low_cardinality_text

Reads: data. A text column with a handful of distinct values. A candidate.

### json_column

Reads: catalog. A `json` column: `jsonb` can be indexed with GIN and compared
for equality, and `json` can do neither.

### primary_key_overflow

Reads: data. An `integer` primary key whose sequence has used much of the
2,147,483,647 it can hold, read from the sequence. Widening it rewrites the
table and every table referencing it.

### mostly_null_index

Reads: data. A btree index whose leading column ANALYZE's sample found mostly
NULL; a partial index `WHERE column IS NOT NULL` is smaller. A candidate:
whether any query looks up the NULLs is not recorded.

## config

### fsync_off

Reads: settings. `fsync = off`: a crash can corrupt the database. A low note
on Neon and Lakebase, which run every compute so.

### full_page_writes_off

Reads: settings. `full_page_writes = off`: a crash mid-write can tear a page.
A low note on Neon and Lakebase.

### autovacuum_off

Reads: settings. Autovacuum is off for the whole server.

### work_mem_spills

Reads: workload. Queries spilling at least 100 MB to temporary files since
the counters started.

### requested_checkpoints

Reads: workload. More checkpoints requested (WAL filling `max_wal_size`, or
an explicit `CHECKPOINT`) than came round on the timer.

### shared_buffers_cache_misses

Reads: workload. The buffer cache serving under 99% of reads.

### effective_cache_size_default

Reads: data. `effective_cache_size` at its default while the database is
larger than it.

### random_page_cost_default

Reads: workload. `random_page_cost` at the spinning-disk default of 4.0 while
almost every read is served from cache.

### slow_query_log_off

Reads: settings. `log_min_duration_statement = -1`: no statement is ever
logged for being slow.

### idle_transaction_timeout_off

Reads: settings. `idle_in_transaction_session_timeout = 0`: an open
transaction left by a client can stay open indefinitely.

## logs

`pg-debug logs FILE...` reads server logs with no connection: stderr (with
`--prefix` set to the server's `log_line_prefix`, or detected), csvlog and
jsonlog, plain or `.gz`; `--format` names the format where detecting it from
each file is not wanted. Each finding names the `file:line` it was read from.
Messages are matched in English (`lc_messages` C or English). When a kind of
event is absent the report says which setting it depends on, since a quiet
log is not a healthy server.

### deadlock

Reads: log. `ERROR: deadlock detected`, with every process in the cycle from
its DETAIL (what each waited for, behind whom, running what) and the
relations named: by name from the CONTEXT, or as `relation OID of database
OID`, which `SELECT OID::regclass` in that database names. Always logged.

### lock_wait

Reads: log. A wait past `deadlock_timeout`, from `still waiting for` and
`acquired` lines, with its holders and how it ended: `acquired`, `deadlock`,
`cancelled` (a statement timeout or a user ended it) or `unresolved`. Logged
only with `log_lock_waits = on`.

### temp_file

Reads: log. Temporary files grouped by the statement that wrote them: count,
total and largest size. Logged only with `log_temp_files` set to a size.

### skipped_vacuum

Reads: log. `skipping vacuum of "t" --- lock not available`: by autovacuum
(LOG), which gave up because something held a lock, or by a manual `VACUUM
(SKIP_LOCKED)` (WARNING). Repeated skips of one table mean its dead rows wait.
Autovacuum logs skips only when `log_autovacuum_min_duration` is not -1.

### autovacuum_run

Reads: log. Autovacuum and autoanalyze runs per table: how many, the longest
and total elapsed time, aggressive and wraparound runs, tuples removed, and
the most dead tuples a run could not remove because the horizon was held.
Logged when a run takes `log_autovacuum_min_duration` (10 minutes by default
from PostgreSQL 15).

### frequent_checkpoints

Reads: log. `checkpoints are occurring too frequently (N seconds apart)`: WAL
filled `max_wal_size` before `checkpoint_timeout`, closer together than
`checkpoint_warning`. The report also counts the checkpoints logged
(`log_checkpoints`) by what started them.
