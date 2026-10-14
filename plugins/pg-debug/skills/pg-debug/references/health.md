# Health, workload and configuration

Reading `health`, `top-queries` and `config`, and putting them in CI with a baseline.

## Health checks

```bash
pg-debug health                          # everything
pg-debug health --type index,vacuum      # a subset
pg-debug --json health                   # structured, for scripts
pg-debug health --exit-code              # exit 3 if a check finds a problem, 1 if one could not run (CI)
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
that has only read holds nothing and is not reported — so when it does report a
backend, that backend really is the cause. "Autovacuum backlog" separates three
different problems that look alike: a table past its threshold and waiting, a
table with autovacuum switched off, and a large table whose default scale
factor means it waits for a fifth of itself to die first. The fixes differ, so
relay which one it is.

Interpret before relaying. "Unused index" on a database that restarted
yesterday means the counters are young, not that the index is dead — check
`metrics.max_scans` and how long statistics have been accumulating. Cache hit
rate on an idle test database is meaningless.

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

Needs `pg_stat_statements`. Query text comes back normalized (`$1` for
literals), which is what you feed to `tune-queries`.

## Reviewing the server's configuration

```bash
pg-debug config              # settings judged against what this server is doing
pg-debug --json config
pg-debug config --exit-code  # exit 3 on a durability setting that is off; Neon's fsync is a note (platforms.md)
```

Every finding is argued from a counter, and the `evidence` field carries it —
quote that rather than the recommendation on its own, because it is what makes
the suggestion checkable. An empty result means nothing *measurable* is wrong,
which is not the same as optimal; say it that way.

It deliberately does not guess at hardware, so where the right value depends on
the machine's memory or storage the finding offers no number and says why. Do
not fill that gap with a number of your own — suggest measuring again after a
change instead.
