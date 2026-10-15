# Schema design, migrations and partitioning

Reading `model`, `check-migration` and `partitions`, and running a partitioned table once it exists: what to relay, and what not to apply on trust.

## Reviewing the data model

Everything else here asks what the server is doing. This asks whether the
schema is built the way the data behaves.

```bash
pg-debug model                           # every rule, every user schema
pg-debug model --schema public
pg-debug model --rule unindexed_foreign_key
pg-debug model --proven-only             # drop what was inferred from statistics
pg-debug model --min-rows 10000          # statistics rules skip smaller tables (default 1000)
pg-debug --json model                    # structured, with the DDL to apply
pg-debug model --exit-code               # exit 3 on a high-severity finding (CI)
```

Rules: `unindexed_foreign_key`, `foreign_key_type_mismatch`,
`missing_primary_key`, `column_order_padding`, `naive_timestamp`,
`blank_padded_char`, `nullable_never_null`, `low_cardinality_text`,
`json_column`, `primary_key_overflow`, `mostly_null_index`, `all`.
Needs no extension, and each rule is one catalog query, so it is safe to point
at production.

**Read the `confidence` field before acting on anything.** A `proven` finding
is a catalog fact — a foreign key either has an index behind it or it does not.
A `candidate` is inferred from sampled statistics and can be wrong: a column
with no NULLs in the sample may still have one, and an enum built from sampled
values will fail to apply if the sample missed a value. Every candidate carries
a `verify` query. Run it and report what it returns before proposing the DDL —
do not present a candidate as a fact.

`severity` already accounts for the evidence, so do not re-rank by rule name.
An unindexed foreign key is `high` only when the child table is large *and* the
parent has recorded deletes; on a small table the same finding is `low` and is
not worth interrupting anyone about. `remediation` is ready to run, but read it
first: several rules emit statements that rewrite the table under an ACCESS
EXCLUSIVE lock, and they say so in `detail`.

`column_order_padding` is the one whose numbers can be checked. It predicts the
page count of the reordered table exactly when every column is fixed-width, and
`evidence` carries `current_pages` and `packed_pages` so the claim can be
tested rather than believed. Its remediation is a `CREATE TABLE` sketch, not a
migration: constraints, defaults, indexes and triggers are not reproduced, and
saying so is part of relaying it honestly.

`primary_key_overflow` is proven: it reads the sequence's last value, and
says how much of the type (or of a sequence still `AS integer` behind a
`bigint` column) is used. Relay the share and the deadline it implies; the
remediation rewrites the table and every integer column referencing it, so on
a large table propose the batched new-column migration the detail mentions
rather than the in-place `ALTER`. A sequence the role may not read, or one
never called, is not reported. `json_column` and `mostly_null_index` are
candidates: run the `verify` query, and for `mostly_null_index` also ask
whether any query looks for `column IS NULL` or reads the column in index
order with no condition on it, the two things the partial index cannot serve.

An empty result means the model has nothing wrong with it that these rules can
see — say that, rather than reaching for something to report.

## Before a major upgrade, and auditing security

```bash
pg-debug upgrade-check --to 18           # what pg_upgrade refuses, drops or leaves to do
pg-debug security                        # who can get in, and what they can do there
pg-debug security --exit-code --baseline security-baseline.json   # CI: fail on a new high finding
```

Both print their findings, then one line per check: `clear`, `found`,
`unknown` (it could not read what it needed, and says why) or `n/a`. **Relay
the `unknown` lines.** Password hashes (`md5_passwords`) and `pg_hba.conf`
(`hba_rules`) are readable by a superuser only; for any other role, and on
most managed platforms, those checks cannot tell, and an audit that skipped
them is not a pass. The report's last line says so.

`upgrade-check` reads one database. `reg_type_columns`, `extension_versions`
and `statistics_after_upgrade` cover only the connected one, and the report
names the others to run it against. What it checks is `pg_upgrade` 18's own
list: `reg*` columns it refuses (`regclass`, `regrole` and `regtype` are fine),
prepared transactions, a `data_checksums` setting the target's initdb default
does not share (18 enables them; `initdb --no-data-checksums` or
`pg_checksums --enable` fixes it), logical slots and subscriptions on a server
before 17 (silently dropped), extended statistics (not carried by 18, so
`ANALYZE` after), extensions behind their default version, and MD5 passwords
(deprecated in 18). It is not `pg_upgrade --check`: recommend running that
too, from the new binaries, before the upgrade.

`security` findings are proven except `rls_coverage`, a candidate: a table
without row-level security, granted to other roles, beside tables in the same
schema that have it. Its `verify` lists who may read the table; ask whether it
is meant to be readable in full before proposing a policy. `rls_without_policy`
is often deliberate (a table kept to a service role), so present it as a
question. `version_currency` compares the server with releases pg-debug knew
of on the date it states; repeat that date, because a newer minor release
makes a "current" verdict wrong.

## Reviewing a migration before it runs

```bash
pg-debug check-migration 001_add_column.sql   # a file
pg-debug check-migration "ALTER TABLE orders ALTER COLUMN id TYPE bigint"
pg-debug check-migration --offline --exit-code 001.sql   # CI, no database needed
pg-debug --json check-migration 001.sql
```

Each statement reports the lock it takes, **how long it holds it**, whether it
rewrites the table, and a safer rewrite where one exists. Relay both halves:
"ACCESS EXCLUSIVE" alone frightens people about statements that finish in
microseconds, and reassures nobody about the ones that do not. `duration` is
the field that separates them.

Connected, it reports the real size of each table, and softens severity for a
small one — correct when pointed at production, misleading when pointed at a
development copy. If the database in reach is not the one the migration will
run against, use `--offline` and report the unsoftened verdict.

Connected, it also reports under `timeouts` the `lock_timeout` and
`statement_timeout` each statement would run with, and where each comes from:
a `SET` earlier in the migration (`SET LOCAL` until its transaction ends), the
query context (`--set lock_timeout=2s`), the `ALTER ROLE ... SET` of the role
named with `--role` or in `pg-debug.toml` (the role the migration logs in as),
or the default. A statement waiting for ACCESS EXCLUSIVE, SHARE ROW EXCLUSIVE
or SHARE on an existing table queues every later statement that conflicts with
that lock, since locks are granted in the order asked; with no `lock_timeout`
it carries `lock_timeout_advice`. Relay it with the lock: it is the part of the
risk that does not depend on the table's size.

Facts worth knowing when reading its output, all measured rather than
recalled: `ADD COLUMN ... NOT NULL DEFAULT 0` does *not* rewrite the table on
PostgreSQL 11+; `ADD FOREIGN KEY` takes SHARE ROW EXCLUSIVE and never blocks
readers; `CREATE INDEX CONCURRENTLY` blocks nothing but cannot run inside a
transaction, so it cannot sit in a migration applied atomically. A default of
`now()` is stable and costs nothing; `clock_timestamp()` and `random()` are
volatile and force a rewrite — the tool resolves which through `pg_proc` when
connected.

## Should this table be partitioned?

```bash
pg-debug partitions                                        # every table over 1GB
pg-debug partitions --min-size 500MB                       # look at smaller ones too
pg-debug partitions --min-pruning-share 0.7                # demand a stronger case
pg-debug --json partitions
```

The verdict is decided by `pruning_share` — the fraction of recorded query time
against that table which restricts on the candidate key, and so would still
read one partition instead of all of them. Size is only a filter for what is
worth asking about; it is never the argument.

**Report the refusals.** Every table examined comes back, `recommended` true or
false, and the false ones are usually the more valuable half: a large table
where most of the query time does not filter on any one column would be made
slower by partitioning, and saying so with the number is a real answer. Do not
present the recommended ones as though they were the whole output.

`blockers` names the unique constraints and primary keys that would have to
gain the partition key. Always pass those on — they are what makes the
conversion expensive, and widening a key changes what the database will accept.

The `ddl` is the **target shape, not a migration**. It creates a new empty
partitioned table; it does not convert the existing one, which needs a copy, a
constraint reconciliation and a name swap. Say that plainly rather than letting
someone read it as a script to run against a populated table. Suggest
`pg_partman` for creating and rolling partitions afterwards, and read the next
section before relaying any of the conversion.

If nothing is recommended, that is a finding, not a failure to find one.

## Once a table is partitioned

`partitions` judges plain tables only, so on a table that is already
partitioned it says nothing. `health --type partition` is the check for those:

```bash
pg-debug health --type partition
pg-debug --json health --type partition     # each finding's kind, evidence and recipe
```

Each finding has a `kind`, a `severity`, a `confidence` and its `evidence`:

- `premade_partitions`: a range partition on one time column whose last
  partition ends within one partition's width of `now()` (medium), or has
  ended (high). Rows for later times go to the DEFAULT partition, or fail to
  insert with "no partition of relation found for row" when there is none. It
  carries a `recipe` for the next partition, the lock-aware one below. It says
  itself that a table no longer receiving new rows makes this expected; ask
  before relaying it as urgent on an archive.
- `default_partition_rows`: the DEFAULT partition holds rows. Every later
  ATTACH or `PARTITION OF` scans it under ACCESS EXCLUSIVE, and a partition
  whose range covers any of its rows is refused until they are moved out.
- `unique_per_partition` (candidate): a unique index created on partitions
  and on no index of the parent holds within each partition only. Whether
  anyone expects it to be global is the guess; ask.
- `pruning_disabled`: `enable_partition_pruning` is off in the configuration,
  for a role or database (`usename`, `datname`), or in the query context.
- `lock_pressure` (candidate): a query can lock more relations than the fast
  path holds (below). Estimated from `pg_stat_statements`: the share of the
  recorded time on the table that does not filter on the partition key. With
  nothing recorded it is the worst case, said in `evidence.basis`; `confirm`
  says how to count a real query's locks.
- `partition_count`: over 1,000 partitions, or three levels deep.

What goes wrong otherwise shows in plans, in the lock manager and in the DDL
that maintains the partitions, and pg-debug reaches those through `diagnose`,
`explain`, `activity`, `check-migration` and `execute`.

### Adding a partition without blocking the table

`CREATE TABLE ... PARTITION OF` takes ACCESS EXCLUSIVE on the parent, and
scans a DEFAULT partition holding rows under it. `ATTACH PARTITION` takes
SHARE UPDATE EXCLUSIVE on the parent, ACCESS EXCLUSIVE on the table being
attached and on the DEFAULT, and scans both unless a validated CHECK on the
new table implies the bound and one on the DEFAULT excludes the range
(measured on PostgreSQL 16). The recipe `premade_partitions` and the
`partitions` sketch give is that: create the table `LIKE` the parent, add the
CHECK `NOT VALID`, load it, `VALIDATE` it (no blocking), the same exclusion
on the DEFAULT, attach, drop the CHECKs. `check-migration` reports every step
of it at low severity, and reports `CREATE TABLE ... PARTITION OF` with the
recipe as its safer form, and `DETACH PARTITION` with `CONCURRENTLY`
(PostgreSQL 14 and later, outside a transaction block, not with a DEFAULT
partition). A VALIDATE on the DEFAULT fails if it holds rows for the new range:
that is the rows to move first, not a reason to skip the CHECK.

### Pruning

Pruning "is driven only by the constraints defined implicitly by the partition
keys, not by the presence of indexes". A query prunes only when it restricts
the partition key; an index on another column does not narrow which
partitions it reads. `enable_partition_pruning` (on by default) governs
pruning at plan time and at execution alike, so check it when nothing prunes
anywhere.

The planner handles "up to a few thousand partitions fairly well" only when
typical queries let it prune all but a few. Planning time and memory grow with
every partition left after pruning, each session that touches a partition
loads its metadata into its own memory, and OLTP wants fewer partitions than a
warehouse. Count them per parent:

```bash
pg-debug --access-mode restricted execute \
  "SELECT inhparent::regclass AS parent, count(*) AS partitions FROM pg_inherits GROUP BY inhparent ORDER BY 2 DESC"
```

Then read the plan, with the value written into the query. Pruning happens in
three places, and pg-debug's text output shows each differently:

- **At planning**, from constants: the pruned partitions are simply not in the
  plan.
- **When execution starts**, from values the planner did not have, such as
  `now()` or a prepared statement's parameters: the pruned partitions are not
  listed either, and their count is "Subplans Removed", which pg-debug prints
  only with `--json`.
- **During execution**, from a subquery's result or a value a nested loop
  passes to its inner side: the partitions stay in the plan, and under
  `explain --analyze` the ones never scanned show `Loops: 0` (PostgreSQL's own
  text says `(never executed)`), or fewer loops than the others.

An Append that lists every partition, with nothing removed, for a query that
restricts the partition key, pruned nothing. On PostgreSQL 16, with a
timestamp key `ts`, that was the outcome when the comparison was on an
expression of the key (`ts::date = ...`, `date_trunc('day', ts) = ...`) and
when it called a volatile function (`clock_timestamp()`); comparing the bare
key, with a constant or with `now()`, pruned. Rewrite the predicate to compare
the key itself. `diagnose` reports that shape as `unpruned_partitions`, a
candidate: every partition scanned and filtered on the key, nothing removed,
most of them returning no rows.

A generic plan is a different matter. It is planned with no values, so the
planner prunes nothing, and the pruning moves to execution start: a prepared
statement run with a generic plan on PostgreSQL 16 still removed every
partition but one. What it costs is planning for every partition and, as the
PostgreSQL documentation says, a lock on every partition at the start of each
execution, the pruned ones included: the same statement locked all four
partitions with a generic plan and one with a custom plan. That feeds the lock
slot problem below. `plan_cache_mode = force_custom_plan`, set for the role or
the transaction that runs the statement, has every execution planned with its
own values; the server consults it when a cached plan is executed, not when it
is prepared. A prepared statement is planned custom for its first five
executions and may switch to a generic plan after that.

Do not read pg-debug's own generic plan as the application's. Given a query
with `$1` placeholders, `explain` plans it generically on PostgreSQL 16 and
later (unless the query has a `LIKE`), with no value at all, so it lists every
partition even where the application's executions prune at execution start.
To see whether the comparison can prune, write a value in.
[slow-queries.md](slow-queries.md) has the rest on generic plans.

### Lock slots

Before PostgreSQL 18 each backend has 16 fast-path lock slots. A query that
touches more relations than that, counting partitions and their indexes, takes
the rest of its locks through the shared lock manager, and at concurrency above
the vCPU count that shows as `LWLock:LockManager` waits. AWS puts it
plainly: the wait follows the number of locks that miss the fast path, not the
number of partitions or indexes in the schema. PostgreSQL 18 sizes the fast
path from `max_locks_per_transaction` (default 64); benchmarks on 18 found
LockManager contention with it set to 16 and none at 64 or 128. Changing it
needs a restart.

One `activity` snapshot with `LWLock:LockManager` waits is an anecdote;
several in a row are a pattern. Count the locks that missed the fast path:

```bash
pg-debug activity
pg-debug --access-mode restricted execute \
  "SELECT pid, count(*) FILTER (WHERE NOT fastpath) AS slow_path_locks, count(*) AS locks FROM pg_locks WHERE locktype = 'relation' GROUP BY pid ORDER BY 2 DESC"
```

The remedies, in order: make the query prune; drop indexes nothing uses
(`health --type index`, read with its counting window in mind); before 18,
keep each hot query under 16 relations; pool connections. On PostgreSQL 18,
raising `max_locks_per_transaction` is the remaining lever.

### Changing partitions without long locks

- **ATTACH PARTITION** takes only SHARE UPDATE EXCLUSIVE on the parent, where
  `CREATE TABLE ... PARTITION OF` takes ACCESS EXCLUSIVE. It scans the new
  partition to validate its bounds while holding ACCESS EXCLUSIVE on that
  partition, and with a DEFAULT partition it also scans the default under
  ACCESS EXCLUSIVE. A matching CHECK constraint added beforehand lets it skip
  the scan: on the new table, one matching its bounds; on the default, one
  that excludes the new range. The recipe is to build and load the table, add
  the CHECK, ATTACH, then drop the CHECK. Measured on PostgreSQL 16: a CHECK
  added `NOT VALID` did not count, and ATTACH scanned until the constraint was
  validated; and on a nullable key column the CHECK had to include
  `key IS NOT NULL` before ATTACH would skip the scan.
- **DETACH PARTITION** takes ACCESS EXCLUSIVE on the parent. `DETACH
  PARTITION ... CONCURRENTLY`, since PostgreSQL 14, takes SHARE UPDATE
  EXCLUSIVE instead.
- **Indexes.** `CREATE INDEX` on a partitioned table cannot be CONCURRENTLY.
  Create it `ON ONLY` the parent, build each partition's index with `CREATE
  INDEX CONCURRENTLY`, and attach each with `ALTER INDEX ... ATTACH PARTITION`.
- **Unique constraints and primary keys** must include every partition-key
  column, with no expressions. `partitions` lists the ones that would have to
  change as `blockers`.

Put each step through `check-migration`, knowing which of them it reviews:

- `ADD CONSTRAINT ... CHECK` it reports as a scan under ACCESS EXCLUSIVE, and
  offers the `NOT VALID` form with a `VALIDATE CONSTRAINT`. On a table nothing
  reads yet, that lock blocks nobody. If you take the `NOT VALID` form, keep its
  `VALIDATE CONSTRAINT` before the ATTACH, or the ATTACH scans anyway.
- `CREATE INDEX CONCURRENTLY` on a partition and `DROP CONSTRAINT` it reviews
  correctly.
- `CREATE INDEX` on a partitioned table, against the database, it reviews as
  a build on every partition, refuses `CONCURRENTLY` on it as PostgreSQL does,
  and writes out the `ON ONLY` recipe with the partitions' names. `ON ONLY` on
  its own it reviews as the catalog update it is. Offline it cannot tell a
  partitioned table, and says so. A build from before this offered a
  `CONCURRENTLY` rewrite for the partitioned table, which PostgreSQL refuses;
  do not relay that.
- `ATTACH PARTITION`, `DETACH PARTITION`, `CREATE TABLE ... PARTITION OF` and
  `ALTER INDEX ... ATTACH PARTITION` it does not review at all: they are missing
  from its output and from its statement count. Its silence on them is not a
  verdict, so relay their locks from the list above.

### pg_partman

pg_partman 5 uses native partitioning only, having dropped trigger-based
partitioning in 5.0.1, and needs PostgreSQL 14 or later. Its defaults decide
what goes wrong:

- `premake` is 4: it creates four partitions ahead.
- `infinite_time_partitions` is false, so no new time partitions are made
  while no new data arrives.
- Its background worker, `pg_partman_bgw`, needs `shared_preload_libraries`
  and runs maintenance every 3600 seconds by default.
- `retention_keep_table` is true, so expired partitions are detached, not
  dropped.

Rows with no partition to go to land in the default partition.
`check_default()` reports them, and `partition_data_time()`,
`partition_data_id()` and `partition_data_proc()` (in batches) move them out.
Its documentation warns that "excessive rows in the default or any unbounded
table can greatly affect the partition maintenance performance". pg-debug
reads nothing of pg_partman's, so run `check_default()` before advising on a
pg_partman set.

The documentation adds three cautions: run it in UTC; unique indexes from the
template table apply per partition, so duplicates across the set are possible
(`check_unique_constraints.py` looks for them); and sub-partitioning gives
little benefit for most workloads, can exhaust `max_locks_per_transaction` and
does not support logical replication publications.

### What partitioning cannot fix

Partitioning "does not remove a single-primary write bottleneck", and it
cannot let the data outgrow one instance. When the primary is limited by
writes, the question is a different one; see [scale-out.md](scale-out.md).
