# Health, workload and configuration

Reading `health`, `top-queries` and `config`, and putting them in CI with a baseline.

## Health checks

```bash
pg-debug health                          # everything
pg-debug health --type index,vacuum      # a subset
pg-debug --json health                   # structured, for scripts
pg-debug health --exit-code              # exit 3 if a check finds a problem, 1 if one could not run (CI)
pg-debug health --min-window-days 30     # call an index unused only after a month of counting
pg-debug health --save-baseline b.json   # accept today's findings
pg-debug health --exit-code --baseline b.json   # then fail only on new ones
```

`model` and `config` take the same `--baseline` / `--save-baseline`. A
baseline holds what each finding is about, not its numbers, so it survives a
bloat percentage drifting; a finding that disappears is simply not listed.
Suggest it whenever a user wants to put one of these checks in CI against a
database that already has findings. A run in which a check could not run saves
no baseline and exits 1: run it again once the lock or permission that stopped
the check is gone.

Checks: `index` (invalid, duplicate, bloated, unused), `table` (heap bloat),
`connection` (client backends against `max_connections` less its
reservations, flagged at 80%, and each session idle in a transaction for a
minute or more, named with its pid, role, database, application and last
statement), `vacuum` (transaction ID wraparound, autovacuum backlog, and what
is holding the vacuum horizon), `sequence` (approaching
maximum), `replication`, `buffer` (cache hit rates), `constraint` (invalid
constraints), `statistics` (never analyzed or drifted), `scan` (tables read
sequentially far more than through an index, charged in bytes read), `all`.

In JSON each check has `healthy`, `summary`, `findings` (the flagged objects)
and `metrics` (the numbers and thresholds behind the verdict). A check that
could not run (a statement timeout behind a lock, a permission it lacks) also
has `error`, with `healthy` false and no findings; relay it as not checked,
not as a problem found. Filter on `healthy` rather than parsing prose:

```bash
pg-debug --json health | jq 'to_entries | map(select(.value.healthy == false))'
```

That also selects checks with an `error`.

`table` estimates bloat from `pg_stats` and `pg_class`, so it needs no
extension, and reports a table only when it wastes at least 100 MB *and* at
least 20% of its size. It models column alignment and `fillfactor`, so a
healthy table estimates exactly — but it reads a few percent high on tables
with variable-width columns (`text`, `numeric`), where `pg_stats` records only
an average width. `bloat_percent` in the 20-30% range on a busy table is
usually normal churn, not a reason to `VACUUM FULL`. Confirm with `pgstattuple`
before recommending a rewrite, and check the vacuum settings first — bloat that
keeps coming back is an autovacuum problem, not a one-off cleanup.

`health --type vacuum` is what diagnoses that. **Read "Vacuum horizon" before
"Autovacuum backlog"**: if something is holding the horizon, no amount of
vacuuming or autovacuum tuning reclaims anything, and the backlog is a
consequence rather than the problem. The horizon check is deliberately narrower
than the usual "find idle in transaction" advice — a READ COMMITTED transaction
that has only read holds nothing vacuum needs and is not reported there, though
it keeps its locks, which the `connection` check flags after a minute — so when
it does report a
backend, that backend really is the cause. "Autovacuum backlog" separates three
different problems that look alike: a table past its threshold and waiting, a
table with autovacuum switched off, and a large table whose default scale
factor means it waits for a fifth of itself to die first. The fixes differ, so
relay which one it is.

Interpret before relaying. The counters behind `index`, `scan` and
`statistics` count since a reset or since PostgreSQL last discarded them (a
crash, an immediate shutdown, a failover, a restore), not since the last
clean restart; each says since when in its summary and in
`metrics.counting_since`. The unused-index check does not call anything
unused until they have counted for a week, and says "Index use not judged"
instead; `--min-window-days` moves that, which is for a database whose
traffic repeats faster, not for getting an answer sooner. On 16 and later a
flagged index carries `last_scan`. Cache hit rate on an idle test database
is meaningless.

pg_debug marks every statement it runs with a `/* pg_debug */` comment and
excludes marked statements from `top-queries` and from index tuning, so its own
catalog reads never appear in its own reports. Queries you run *through* it —
`pg-debug execute`, `explain` — are marked too, so do not expect them to turn
up in `top-queries` afterwards.

## Finding expensive queries

```bash
pg-debug top-queries                                  # blended resource cost
pg-debug top-queries --sort-by mean_time --limit 5    # slowest per call
pg-debug top-queries --sort-by total_time --limit 5   # biggest total consumer
pg-debug top-queries --interval 10                    # measure the next 10s only
```

`resources` blends execution time, buffer access and WAL volume, and reports
everything above a threshold — `--limit` does not apply to it in cumulative
mode. Use `mean_time` to find individually slow queries and `total_time` to
find what the database actually spends its life doing; they often disagree, and
the disagreement is the interesting part.

**Prefer `--interval` when the question is about now.** Without it the counters
are cumulative since statistics were last reset, so the ranking is often
dominated by a migration or a bulk load that ran once, months ago. Sampling
takes two snapshots and reports the difference — the live workload, with
`mean_exec_time` recomputed over the interval. A few seconds is enough on a
busy server; on a quiet one, sample longer or you will rank noise.

Needs `pg_stat_statements` in the database you connected to, in any schema
(Supabase's `extensions` is found even with a `--search-path` that leaves it
out). Only that database's statements are ranked, though the extension records
every database's. Query text comes back normalized (`$1` for
literals), which is what you feed to `tune-queries`. As a role without
`pg_read_all_stats` only its own statements are ranked, since other roles'
queryid is withheld; the command says on stderr how many ran.

**Changed, not merely large.** A sample also marks each statement's `change`:
`new` when it was not in pg_stat_statements before the sample, `slower` or
`faster` when its mean time in the sample is at least 3 times, or a third of,
its mean over the calls recorded before it. A statement at the top of the
ranking may always have been there; a changed one is the place to look first.
Both are candidates, inferred from two snapshots: confirm one with
`diagnose`. They are judged only when pg_stat_statements has counted at least
ten samples' length before the sample (and, from pg_stat_statements 1.11, the
statement itself has); otherwise stderr says why nothing was judged, and a
`new` statement says when evictions (`pg_stat_statements.max`) may explain it.

## What the server is doing per second

```bash
pg-debug server-rates --interval 10
pg-debug --json server-rates | jq '.groups[] | select(.measures == "database")'
```

Two snapshots of the server's own counters, each reported as a delta and a
rate per second over the interval measured between that view's two reads:
`pg_stat_database` for this database (commits, rollbacks, blocks read and
hit, tuples, temp files and bytes, deadlocks, conflicts), how far WAL was
written (or replayed, on a standby), `pg_stat_wal` (14+), the checkpointer
and background writer (`pg_stat_checkpointer` from 17), `pg_stat_io` (16+) by
process type and object, with the rows that did no I/O counted rather than
listed, and every replication slot's retained WAL and how fast it grows
(negative when its consumer catches up).

A view whose recorded reset moved during the interval is refused, with both
times, and so is any counter that went backwards, with both values: a delta
across a reset is a negative or a too-small rate, not a measurement. Run it
again. A view the version lacks says which version brought it. Backends
report their counters about once a second, so a few seconds misses some of
what ran at the edges; ten is the default. The database's counters include
one transaction of pg-debug's own.

## Reviewing the server's configuration

```bash
pg-debug config              # settings judged against what this server is doing
pg-debug --json config
pg-debug config --exit-code  # exit 3 on a durability setting that is off; on Neon and Lakebase a note (platforms.md)
```

Every finding is argued from a counter, and the `evidence` field carries it —
quote that rather than the recommendation on its own, because it is what makes
the suggestion checkable. An empty result means nothing *measurable* is wrong,
which is not the same as optimal; say it that way.

It deliberately does not guess at hardware, so where the right value depends on
the machine's memory or storage the finding offers no number and says why. Do
not fill that gap with a number of your own — suggest measuring again after a
change instead.
