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
statement), `vacuum` (transaction ID wraparound, autovacuum backlog, what
is holding the vacuum horizon, and multixact wraparound), `sequence` (approaching
maximum), `replication` (lag, and what each slot retains), `buffer` (cache hit
rates), `constraint` (invalid constraints), `statistics` (never analyzed or
drifted), `scan` (tables read sequentially far more than through an index,
charged in bytes read), `collation` (collation versions that no longer match
their library, with the indexes on them), `checksum` (pages that failed their
checksum), `all`.

"Multixact wraparound" flags a database or table whose oldest multixact is
past 200 million (`level: approaching`), past
`autovacuum_multixact_freeze_max_age` or its per-table override
(`past_freeze_max_age`: autovacuum is not keeping up), or within 10 million of
the hard limit (`wraparound`); tables are those of the database connected to.
It never gives a member-space percentage, because PostgreSQL publishes none.
Unrestricted, as a role allowed `pg_ls_dir` and `pg_stat_file`, it measures
`pg_multixact/members` on disk and reports past 10 GB a `member_space`
finding with `confidence: candidate`; otherwise it says the size was not read.
`metrics.member_slru` (13 and later) is how busy member storage has been, not
how full.

"Replication slot retention" reports each slot once, under its worst `kind`:
`lost`, `unbounded` (inactive with `max_slot_wal_keep_size = -1`, or before
13: it retains until the disk fills), `unreserved`, `inactive`, `extended`
(connected but behind by more than `max_wal_size`) and `horizon` (an `xmin` or
`catalog_xmin` older than `autovacuum_freeze_max_age`). `retained_bytes` is
measured from `pg_current_wal_lsn()`, on a standby from
`pg_last_wal_replay_lsn()`. Slots it did not flag are in `metrics.examined`.
Never advise dropping a slot without confirming its consumer is gone (see
[scale-out.md](scale-out.md)).

"Collation versions" and "Data checksums" are covered in
[incidents.md](incidents.md): a changed collation version proves the library
changed, not that an index is corrupt, and checksums off means failures
cannot be counted, which the check says rather than calling it clean.

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

`resources` blends execution time, buffer access and WAL volume: it lists the
queries over 5% of any one resource, the largest share first, up to `--limit`.
Use `mean_time` to find individually slow queries and `total_time` to
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
