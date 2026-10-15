# The database is stuck

Reading `activity`, `locks` and `vacuum-progress` when something is wrong right
now, three incidents that are easy to misread, and what the waits in one
snapshot do and do not say.

When it already happened and is over, the server log holds what the views
no longer show: `pg-debug logs postgresql.log` (no connection) lists the
deadlocks with both processes and their queries, the lock waits past
`deadlock_timeout` with their holders and how each ended, temporary files per
statement, vacuums skipped for a lock and slow autovacuum runs. Relay its
notes too: a lock wait is only logged with `log_lock_waits = on`, so a log
without any says nothing unless that was on. The `logs` section of
[rules.md](rules.md) has the detail.

## Something is wrong right now

When the complaint is "the database is stuck" rather than "this query is slow",
start with what is actually running:

```bash
pg-debug activity                    # everything doing something
pg-debug activity --blocked          # only backends waiting on a lock
pg-debug activity --min-duration 30  # anything stuck in its current state 30s+
pg-debug vacuum-progress             # is that vacuum going to finish?
```

`activity` hides idle backends by default and excludes its own connection.
Each row carries `blocked_by` — the pids holding the lock it is waiting for —
and `blocking_total`, how many backends wait on it directly or behind others
that do. Culprits come first: root blockers (blocking, waiting on nothing),
the one blocking the most first, then blockers that wait themselves, then
the waiters, who are victims: ending one frees nothing.

When backends are waiting on each other, `locks` does the tracing:

```bash
pg-debug locks                       # who is blocking whom, root blockers first
pg-debug --json locks                # the same tree as data
```

It walks `blocked_by` back from every waiter to a backend that waits on
nothing and prints that root first, with everything stuck behind it nested
beneath. Each waiter line says what it waits for — the lock mode and table, or
for a row lock, the other transaction and the table the row is in — and what
the holder has on it. The root is usually `idle in transaction`: an
application that opened a transaction, took a lock, and went off to do
something else. The query shown for it is the last one it ran before going
idle, which is how you find that code. `blocking N` on the root is how many
backends are stuck behind it in total, so when there are several roots the
one listed first is the one to deal with. A root marked as a deadlock is in a
cycle the server will break within `deadlock_timeout`.

`lock_tree()` returns the same trees from Python: each node has `pid`,
`state`, `query`, the `*_seconds` ages, `blocked_by`, `blocking` (nested
nodes), `blocked_total`, and on a waiter `waits_for` with `locktype`, `mode`,
`relation`, `tuple_relation`, `transactionid`, `held_modes` and a
`description`.

Run these as a role with `pg_read_all_stats`, which `pg_monitor` includes.
Without it, other roles' sessions come back with no state, wait or timings
and `<insufficient privilege>` for the query. Whether each is in a
transaction, its locks and who it blocks still show. `activity` lists the ones
in a transaction as `in a transaction` with their locks, and leaves out the
rest, whose activity cannot be told (`--all` lists them). `locks` draws every
tree whole, and `activity --blocked` describes a withheld blocker. Each says
on stderr, also with `--json`, how many were withheld and the statement that
shows them: `GRANT pg_monitor TO <role>`, or for a member that does not
inherit it `... WITH INHERIT TRUE` (16+) or `ALTER ROLE <role> INHERIT` (15).
To check by hand:

```bash
pg-debug execute "SELECT pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')"
```

Behind a connection pooler, `activity` shows the server connections the pooler
holds, not the clients queued inside it. An application that times out while
`activity` looks calm may be waiting for a full pool, which only the pooler's
own counters show (PgBouncer's `SHOW POOLS`: `cl_waiting`, `maxwait`).
`pg-debug pooler ADMIN_URL` reads them; [platforms.md](platforms.md) says how.

`vacuum-progress` pairs with the vacuum health check: the check tells you
wraparound is approaching, this tells you whether the vacuum fixing it is
moving. Watch `percent_scanned` across two calls rather than guessing from one.

## Three incidents that are easy to misread

### A migration that took the table offline

Many `ALTER TABLE` forms need ACCESS EXCLUSIVE on the table, and
`check-migration` says which. While such a statement waits for that lock, it
blocks every query on the table that arrives after it, plain reads included.
A change that would finish in milliseconds stops all traffic to the table for
as long as the transaction holding the table stays open, and that is usually a
long or idle transaction that touched the table earlier.

`locks` shows the chain in that order. The root is the holder. Under it is the
DDL, waiting for `AccessExclusiveLock on public.orders (<pid> holds
AccessShareLock)`. Under the DDL are the queued queries, each waiting for a
lock the DDL does not hold yet, so their lines name no mode held by their
parent. In `activity` all of them wait on `Lock:relation`, and the queued
queries' `blocked_by` is the DDL's pid, not the holder's. They are victims;
the holder and the DDL are the two things that can be acted on.

Ask before ending anything. Cancelling the DDL frees the queries queued behind
it at once; ending the holder lets the DDL through, and the queue after it.

For the next attempt, the migration should set a short `lock_timeout`, so the
DDL gives up instead of queueing, and retry with backoff and jitter, for
example `SET lock_timeout = '50ms'` (`SET LOCAL` inside the migration's
transaction when it runs through a transaction pooler, so the setting does not
stay behind for the application). PostgreSQL does not retry for you, and a
`lock_timeout` at or above a nonzero `statement_timeout` does nothing useful.
`check-migration` reports the lock each statement takes and how long it holds
it. Connected, it also reads the `lock_timeout` each statement would run with:
a `SET lock_timeout` earlier in the file, the query context's (`--set
lock_timeout=...`), the `ALTER ROLE ... SET` of the role the context names
(`--role`, which is how the migration logs in), or the default, and says which.
For a statement that waits for ACCESS EXCLUSIVE, SHARE ROW EXCLUSIVE or SHARE on
an existing table with none set, it advises one (`lock_timeout_advice`); its
`5s` is a ceiling, and a busy table wants the shorter value above. Before
running DDL on a busy table, look for transactions open longer than about a
minute:

```bash
pg-debug --json activity | jq '.[] | select(.transaction_seconds > 60) | {pid, state, transaction_seconds, query}'
```

### Multixact member space running out

Multixacts have a wraparound of their own, separate from transaction IDs, and
it has two limits. One is age, watched the way transaction ID age is, against
`autovacuum_multixact_freeze_max_age` (400 million by default). The other is
the member storage behind them, which runs out at about 4 billion members,
roughly 20 GB, whatever the age says; the current documentation says that
once it passes about 10 GB, every table is vacuumed aggressively more often
(older documentation states the trigger differently). Age-based monitoring
misses the second limit. In one reported case, a backfill of
inserts with foreign-key references caused four outages from member-space
exhaustion while monitoring showed multixact age under 50%; emergency vacuums
began near 200 million although the setting was 400 million, and recovery took
hours on large tables.

`health --type vacuum` reads them in its "Multixact wraparound" check: each
database's age and this database's oldest tables, against an alert age of 200
million, `autovacuum_multixact_freeze_max_age` and the hard limit, in
restricted mode too (see [health.md](health.md)). A build from before that
check covers transaction IDs only, so a clean result there says nothing about
multixacts; read the ages yourself, in the default mode:

```bash
pg-debug --timeout 10 execute "SELECT datname, mxid_age(datminmxid) AS mxid_age FROM pg_database ORDER BY 2 DESC"
pg-debug --timeout 10 execute "SELECT oid::regclass AS table_name, mxid_age(relminmxid) AS mxid_age FROM pg_class WHERE relkind IN ('r', 'm', 't') ORDER BY 2 DESC LIMIT 10"
```

PostgreSQL exposes no member-space percentage, so do not report one. The
nearest evidence is the size of `pg_multixact/members` on disk, which the
check reads when it may and reports past 10 GB as a candidate. Reading it
needs a superuser, or a role granted EXECUTE on `pg_ls_dir` and
`pg_stat_file`; `pg_read_server_files` alone is refused. Where the role has
neither, say member space could not be read. The `pg_stat_slru` rows for
multixact members and offsets (PostgreSQL 13 and later; named
`MultiXactMember` and `MultiXactOffset` up to 16, `multixact_member` and
`multixact_offset` from 17) show how busy that storage is, not how full, and
can be read in restricted mode.

```bash
pg-debug --timeout 10 execute "SELECT count(*) AS files, pg_size_pretty(sum((pg_stat_file('pg_multixact/members/' || f)).size)) AS size FROM pg_ls_dir('pg_multixact/members') AS f"
```

Alert near 200 million rather than at the setting. When member space is the
problem, more autovacuum workers and a vacuum that skips index cleanup
(`VACUUM (INDEX_CLEANUP OFF)`) get through large tables sooner, and the lasting
fix is in the schema: audit foreign keys that point at low-cardinality tables.
Unlike transaction ID cleanup, multixact cleanup is not directly held back by
replication slots, so a slot is not the first suspect. Watch the vacuum with
`vacuum-progress`.

### Text indexes that miss rows after an OS upgrade

A B-tree index on a text column is ordered by its collation, and a libc
collation's rules come with the operating system. When an OS upgrade changes
the C library's locale data, indexes and range partitions on text keys built
under the old rules stop agreeing with the new ones: a query can fail to find
a row that is there, a duplicate can get past a unique index, and a row can
land in the wrong partition. No query fails. The change in glibc 2.28 is the
best known; the affected upgrades include Debian 10, Ubuntu 18.10 and later,
RHEL 8 and later, SLE 15 SP3, and macOS 11. The C and POSIX locales, ICU
collations, `bytea` and numeric keys are not affected by a glibc change, nor is
a database moved with `pg_dump` or logical replication. A physical standby
running a newer OS than its primary is affected, and there the inconsistency
can generally only be detected on the standby.

`pg-debug health --type collation` compares the versions. PostgreSQL 15 and
later record each database's collation version, and other collations have
carried recorded versions since PostgreSQL 13 for glibc and 10 for ICU. The
check compares every database's default (15 and later) and each collation an
index in the connected database uses with what the library reports now, and
for a mismatch names the dependent indexes (for another database's default:
connect to it) and the fix, `REINDEX INDEX CONCURRENTLY` on each and then
`ALTER COLLATION ... REFRESH VERSION` or `ALTER DATABASE ... REFRESH
COLLATION VERSION`, in that order: refreshing first silences the warning and
leaves the indexes as they were. It runs in restricted mode. It does not check
index structure. A build from before the check needs these, in the default
mode:

```bash
pg-debug --timeout 10 execute "SELECT datname, datcollversion, pg_database_collation_actual_version(oid) AS actual FROM pg_database WHERE datcollversion <> pg_database_collation_actual_version(oid)"
pg-debug --timeout 10 execute "SELECT collname, collversion, pg_collation_actual_version(oid) AS actual FROM pg_collation WHERE collversion <> pg_collation_actual_version(oid)"
```

A row from either is a proven mismatch, though the second lists every
mismatched collation, including ones no index uses. A version that was never
recorded is empty and is left out, because it proves nothing either way. No
rows proves nothing before PostgreSQL 15, which never recorded the database
default's version; there the evidence is the OS upgrade history and amcheck. `bt_index_check` takes the
same lock as a `SELECT` and is the one to run on production;
`bt_index_parent_check` blocks writes and cannot run on a hot standby. Either
can only prove corruption is present, never that it is absent. They need the
amcheck extension, so ask before installing it, and restricted mode refuses
them:

```bash
pg-debug execute "SELECT bt_index_check('public.users_email_idx'::regclass)"
```

The fix is to rebuild the indexes on `text`, `varchar`, `char` and `citext`
columns and to check range partitions keyed on those types. A unique index
that let duplicates in will not rebuild until they are resolved. On
PostgreSQL 15 and later, `ALTER DATABASE ... REFRESH COLLATION VERSION` then
records the new version; run before the rebuild, it only silences the check.
PostgreSQL 17's builtin collation provider, with its `C` and `C.UTF-8`
locales, does not depend on the operating system at all.

Corruption below PostgreSQL is a different matter: a page that changed on
disk fails its checksum when read, if data checksums are on.
`pg-debug health --type checksum` reads `pg_stat_database.checksum_failures`
and `checksum_last_failure` (PostgreSQL 12 and later) per database and for
the shared catalogs, with since when they have counted, and says so when
`data_checksums` is off and nothing can be counted; off is how most servers
before PostgreSQL 18 were created, so it is not reported as a problem. A
failure points at storage or memory: the server log names the page, and the
backup to keep is one taken before the first failure.

## Reading waits from one snapshot

`activity` prints each backend's wait as `type:event`, `Lock:relation` for
example. The type says what kind of wait it is: `Lock` means another backend,
`IO` means storage, `Timeout` usually means the query is deliberately
sleeping, and `LWLock` means internal contention. A long-running `active`
query with no wait event is simply working.

One snapshot is anecdote: a wait seen once may be gone a millisecond later.
`activity` and `locks` end their text with what the waits in that one snapshot
amount to, each with its pids, lock modes and relation and a confidence: a
**lock queue behind DDL** and an **idle-in-transaction blocker** are proven
from `pg_locks`; **row lock contention** is proven once `pg_locks` shows the
row lock and a candidate before (the same wait is how an insert waits on a
conflicting unique key); **LWLock pressure** (3 or more backends at once on
`BufferMapping` or `LockManager`) is a candidate whose cause is labelled a
judgment, and fewer are listed as seen and not classified. Relay the
confidence with it. `--json` prints the rows alone; the library's
`classify_waits()` returns the diagnosis.

To see a pattern rather than an instant, sample:

```bash
pg-debug activity --sample 10                        # 20 samples, one every 0.5s
pg-debug activity --sample 30 --sample-interval 0.2
pg-debug --json activity --sample 10
```

It reads `pg_stat_activity` once per sample, one statement each, and reports
average active sessions (AAS) overall and for client backends, per wait event
and per query (by `query_id` where the server reports one, by text with
constants replaced where it does not, and it says why). Relay the number of
samples and the interval it achieved, which it measures on the server's clock
and which can be longer than asked for; a wait shorter than the interval shows
only in proportion to how often it happens. Each sample is exact, the profile
is a sample: every attribution is a candidate. `CPU` is an active session
reporting no wait, on the CPU or in a wait PostgreSQL does not instrument.
pg-debug's own backend and statements are left out; a role without
`pg_read_all_stats` is told how many backends' state was withheld, and they
are in no figure. Where `pg_wait_sampling` is installed (it samples every
backend every 10 ms in the server), its profile over the same window is read
too and reported under `extension`, with its own AAS; where Azure's Query
Store records waits, its most recent interval, which is its own, not the
sampled window.

The waits worth recognising:

- **`Lock:relation` behind a waiter for `AccessExclusiveLock`.** The DDL lock
  queue above. The queued queries are victims; deal with the root `locks`
  names, or with the DDL.
- **`Lock:transactionid`.** Waiting for another transaction to end, which is
  how a row lock waits; `locks` names the table the row is in. When the root is
  `idle in transaction`, the application took the row and has not finished its
  transaction, and the query shown is the last one it ran: find that code
  path. Setting `idle_in_transaction_session_timeout` ends such sessions on
  the server's side, and matters beyond locks, because an open idle
  transaction also stops vacuum removing recently dead rows.
- **`LWLock:LockManager`** (`lock_manager` in AWS's documentation). Before
  PostgreSQL 18, each backend has 16 fast-path slots for relation locks in the
  weak modes (`AccessShareLock`, `RowShareLock`, `RowExclusiveLock`). A query
  that opens more relations than that, counting every partition and every
  index, takes the rest through the shared lock table, and with more active
  sessions than vCPUs this wait appears. It follows the number of locks off
  the fast path, not the size of the schema. Count them per backend:

  ```bash
  pg-debug execute "SELECT pid, count(*) FILTER (WHERE NOT fastpath) AS not_fastpath, count(*) AS relation_locks FROM pg_locks WHERE locktype = 'relation' GROUP BY pid ORDER BY 2 DESC LIMIT 10"
  ```

  Rule out `Lock:relation`, `Lock:transactionid` and `Lock:tuple` waits
  first, since they can drive it. The fixes cut locks per query or sessions
  at once: queries that prune to fewer partitions, fewer indexes (the unused
  ones `health --type index` finds, once its counters have been counting long
  enough to mean it), and a pooler in front. PostgreSQL 18 sizes the fast
  path from `max_locks_per_transaction` (64 by default), which takes a
  restart to change. pg-debug does not know the vCPU count; ask for it rather
  than assume one.
- **`LWLock:BufferMapping` and `LWLock:WALWrite`.** These have common readings,
  a working set larger than `shared_buffers` and WAL writes that cannot keep
  up with the commit rate or the disk, but they are not established well
  enough to report as findings. Check them against counters before saying
  either: `health --type buffer` gives cache hit rates, which read low
  wherever a second cache tier sits outside `shared_buffers` (Neon, Aurora
  Optimized Reads, AlloyDB, the Cloud SQL data cache), and
  `top-queries --interval 10` gives each query's WAL bytes. pg-debug does not
  read `pg_stat_io` or `pg_stat_wal` yet, so it cannot say more than that.
