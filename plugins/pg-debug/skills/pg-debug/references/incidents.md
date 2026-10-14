# The database is stuck

Reading `activity`, `locks` and `vacuum-progress` when something is wrong right
now, three incidents that are easy to misread, and what the waits in one
snapshot do and do not say.

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
Each row carries `blocked_by` — the pids holding the lock it is waiting for.

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
Without it, other roles' sessions come back with no state, no wait and
`<insufficient privilege>` for the query. `activity` leaves them out unless
you pass `--all`, so `(nothing running)` can mean nothing you are allowed to
see, and pg-debug does not yet say so. Check before trusting an empty answer:

```bash
pg-debug execute "SELECT pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')"
```

Behind a connection pooler, `activity` shows the server connections the pooler
holds, not the clients queued inside it. An application that times out while
`activity` looks calm may be waiting for a full pool, which only the pooler's
own counters show (PgBouncer's `SHOW POOLS`: `cl_waiting`, `maxwait`).
pg-debug does not read them.

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
it, and advises a short `lock_timeout` for the brief ACCESS EXCLUSIVE changes,
but it does not check whether the migration sets one: a `SET lock_timeout` in
the file changes nothing in its report. Before running DDL on a busy table,
look for transactions open longer than about a minute:

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

pg-debug does not read multixacts. The wraparound check in `health --type
vacuum` covers transaction IDs only, so a clean result there says nothing
about multixacts. Read the ages yourself. Restricted mode does not know
`mxid_age` and refuses these, so they need the default mode, with a timeout:

```bash
pg-debug --timeout 10 execute "SELECT datname, mxid_age(datminmxid) AS mxid_age FROM pg_database ORDER BY 2 DESC"
pg-debug --timeout 10 execute "SELECT oid::regclass AS table_name, mxid_age(relminmxid) AS mxid_age FROM pg_class WHERE relkind IN ('r', 'm', 't') ORDER BY 2 DESC LIMIT 10"
```

PostgreSQL exposes no member-space percentage, so do not report one. The
nearest evidence is the size of `pg_multixact/members` on disk. Reading it
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

pg-debug checks neither collation versions nor index structure yet. PostgreSQL
15 and later record each database's collation version, and other collations
have carried recorded versions since PostgreSQL 13 for glibc and 10 for ICU;
compare them with what the OS provides now. Restricted mode refuses these
functions, so they need the default mode:

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

## Reading waits from one snapshot

`activity` prints each backend's wait as `type:event`, `Lock:relation` for
example. The type says what kind of wait it is: `Lock` means another backend,
`IO` means storage, `Timeout` usually means the query is deliberately
sleeping, and `LWLock` means internal contention. A long-running `active`
query with no wait event is simply working.

One snapshot is anecdote: a wait seen once may be gone a millisecond later.
Run `activity` a few times before calling anything a pattern; pg-debug has no
sampling mode yet. Where `pg_wait_sampling` is installed, it has been sampling
every backend every 10 ms, and its `pg_wait_sampling_profile` view, which
`pg-debug execute` can read, counts waits per process, event and query.

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
