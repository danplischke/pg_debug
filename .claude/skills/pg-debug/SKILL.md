---
name: pg-debug
description: Diagnose and tune a PostgreSQL database with the pg_debug library and its pg-debug CLI — inspect schemas and tables, read EXPLAIN plans, diagnose a slow query's plan and say which node's row estimate went wrong and why, test hypothetical indexes without creating them, see what every backend is doing right now and which one is blocking the others, track vacuum progress, check planner statistics behind bad row estimates, run health checks (bloated/duplicate/unused indexes, table bloat, vacuum wraparound, sequence exhaustion, replication, cache hit rates, invalid constraints, stale statistics), find the most expensive queries from pg_stat_statements, get index recommendations, review the data model itself for design defects (unindexed foreign keys, mismatched key types, missing primary keys, wasteful column order, misleading column types), say whether a table is worth partitioning based on what the workload actually filters on, and review a migration before running it for the locks it takes and whether it rewrites a table. Use this skill whenever the user asks why a Postgres query is slow, why a query's row estimates are wrong, why a query spilled to disk or ran out of work_mem, why the database is stuck or blocked, whether an index would help, what indexes to add, whether their database is healthy, how to improve or review their schema or data model, whether a migration or ALTER TABLE is safe to run or will lock a table, whether a big table should be partitioned and on what key, what tables or columns exist, or wants to script any of that in Python — even if they never mention pg_debug by name. Also use it when writing code against the pg_debug package.
---

# pg_debug

`pg_debug` answers questions about a live PostgreSQL database: what is in it,
why a query is slow, whether an index would help, and whether anything is
quietly rotting. It is a Python library (`PgDebug`, async; `SyncPgDebug`,
blocking) with a `pg-debug` CLI on top.

## Before anything else: connect

Every command needs a connection URL. Prefer the environment variable so the
password stays out of shell history and process listings:

```bash
export DATABASE_URI=postgresql://user:pass@host:5432/dbname
```

`-d/--database-url` overrides it. `$DATABASE_URL` works as a fallback.

**When exploring a database you do not own, add `--access-mode restricted`.**
It parses every statement, rejects anything that is not read-only, runs it in a
read-only transaction and applies a 30 second timeout. It is the difference
between a diagnostic tool and a loaded gun:

```bash
pg-debug --access-mode restricted execute "SELECT count(*) FROM orders"
```

In unrestricted mode there is no timeout unless you ask for one. On a
production database, `--timeout 10` is cheap insurance against an accidental
sequential scan.

## Diagnosing a slow query

Work outside in — plan first, then indexes, then confirm.

```bash
# 1. Ask what is wrong with the plan. Start here — it reads the plan for you.
pg-debug diagnose "SELECT * FROM orders WHERE status = 'open' AND region = 'eu'"

# 2. What is the planner doing?
pg-debug explain "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"

# 3. What would it really do? (runs the query)
pg-debug explain --analyze "SELECT * FROM orders WHERE user_id = 42"

# 4. Would an index help? Nothing is created — HypoPG simulates it.
pg-debug explain "SELECT * FROM orders WHERE user_id = 42" -i 'orders(user_id)'

# Expressions work too, exactly as CREATE INDEX would take them.
pg-debug explain "SELECT * FROM users WHERE lower(email) = 'a@b.c'" -i 'users(lower(email))'

# 5. Or let the advisor search for candidates itself.
pg-debug tune-queries "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"
```

`diagnose` is the one to reach for first when the question is "why is this
slow". It runs `EXPLAIN ANALYZE` and reads the result: the lowest node whose
row estimate diverged from what it found (everything above it inherited that
error), whether the cause is stale statistics or correlated columns with no
extended statistics covering them, sorts and hashes that outgrew `work_mem`,
scans reading rows only to discard them, and parallel workers that were planned
for and not granted. Findings are ranked by the share of the query's work
behind them, and each carries the statement or setting that addresses it.

Because it runs the query it needs `--access-mode unrestricted`, and it says so
rather than half-answering. A statement that writes is run in a transaction
that is rolled back — which undoes the rows but not a sequence it consumed.

When the user already has a plan — pasted into the conversation, from
`auto_explain`, from a replica — do not ask them to run the query again:

```bash
pg-debug diagnose --plan plan.json          # EXPLAIN (ANALYZE, FORMAT JSON) output
pg-debug diagnose --plan - < plan.json
pg-debug diagnose --plan --offline plan.json   # never connect, even with DATABASE_URI set
```

This runs nothing, so it works in restricted mode and without a database. With
a connection it also checks the tables for stale statistics and missing
extended statistics; without one a misestimate says those were not checked.
Only the JSON format is read: a plan pasted in the text format has to be
re-taken with `EXPLAIN (ANALYZE, FORMAT JSON)`, and say so. From Python,
`diagnose_plan(plan)` on a client does the connected version and the top-level
`pg_debug.diagnose_plan(plan)` the offline one, synchronously.

An empty result is a real answer: the estimates matched and nothing spilled.
Say that, rather than reaching for something else to blame.

`--analyze` executes the query, so do not use it for `UPDATE`/`DELETE` unless
you mean it. It also cannot be combined with `-i`.

An index only helps a predicate it matches: to speed up `WHERE lower(email) =
...` the index has to be on `lower(email)`, not on `email`. PostgreSQL will
only accept an expression built from IMMUTABLE functions, so `date_trunc('day',
created_at)` on a `timestamptz` is rejected — that is the server's rule, not a
parsing failure.

Read the plan for the shape of the problem: a `Seq Scan` with a selective
`Filter` wants an index; a huge row-estimate error (`Plan Rows` far from
`Actual Rows`) usually means stale statistics — suggest `ANALYZE` before
suggesting indexes. `diagnose` does both of those readings for you and names
which one applies, so prefer it to eyeballing the plan.

Step 3 is the honest way to answer "would this index help?" — it reports the
planner's cost with the index in place, without touching the database. Compare
`Total Cost` before and after and say what changed rather than asserting an
index is a good idea.

## Recommending indexes

```bash
pg-debug tune-queries "SELECT ..." "SELECT ..."   # up to 10 queries
pg-debug tune-workload --max-index-size-mb 500    # from pg_stat_statements
```

Each recommendation carries an estimated size and the planner cost before and
after, individually and cumulatively. Report those numbers rather than just the
DDL — the improvement multiple is what justifies the write cost of a new index.
The size is what HypoPG measures for the candidate index, and it is also what
`--max-index-size-mb` budgets against, so a small budget really does drop
recommendations.

Two things routinely surprise people:

- **Requires HypoPG.** Without it, tuning returns an `error` explaining how to
  install it. `top-queries` and `tune-workload` additionally need
  `pg_stat_statements`. No superuser is needed — pg_debug uses HypoPG's
  functions, which any role can execute, not its views, which are private to
  the role that created the extension.
- **Unqualified names resolve through the `search_path`.** Schema-qualified
  names (`reporting.orders`) work directly. For unqualified queries against
  another schema, pass `--search-path reporting,public`.

"No index recommendations found" is a real answer, not a failure — it usually
means a suitable index already exists or the planner does not think one helps.
Check `pg-debug describe <table>` before assuming something went wrong.

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

`wait_event_type` says what kind of wait it is: `Lock` means another backend,
`IO` means storage, `Timeout` usually means the query is deliberately sleeping,
and `LWLock` means internal contention. A long-running `active` query with no
wait event is simply working.

`vacuum-progress` pairs with the vacuum health check: the check tells you
wraparound is approaching, this tells you whether the vacuum fixing it is
moving. Watch `percent_scanned` across two calls rather than guessing from one.

## Why the planner got it wrong

When `explain --analyze` shows estimated rows far from actual rows, no index
will fix it — the planner is working from bad numbers. Look at them:

```bash
pg-debug column-stats orders
pg-debug health --type statistics
```

`column-stats` shows what the planner believes per column: `null_frac`,
`n_distinct` (negative means a multiple of the row count, so `-1` is unique),
`correlation` (how closely physical order tracks the column, which decides
whether an index scan looks cheap) and how many most-common values it tracks.
It also lists any extended statistics on the table.

Two common findings, and what they mean:

- **Stale or missing statistics.** `health --type statistics` flags tables
  never analyzed, or where a large fraction of rows changed since. The fix is
  `ANALYZE <table>`, and it is worth doing before any index work.
- **Correlated columns.** If a two-column predicate estimates far too few rows
  and each column's own statistics look right, the planner is multiplying
  independent selectivities for columns that are not independent. That is what
  `CREATE STATISTICS ... ON a, b FROM t` fixes — check `column-stats` first to
  see whether such an object already exists.

## Health checks

```bash
pg-debug health                          # everything
pg-debug health --type index,vacuum      # a subset
pg-debug --json health                   # structured, for scripts
pg-debug health --exit-code              # exit 3 if anything is unhealthy (CI)
```

Checks: `index` (invalid, duplicate, bloated, unused), `table` (heap bloat),
`connection`, `vacuum` (transaction ID wraparound, autovacuum backlog, and what
is holding the vacuum horizon), `sequence` (approaching
maximum), `replication`, `buffer` (cache hit rates), `constraint` (invalid
constraints), `statistics` (never analyzed or drifted), `all`.

In JSON each check has `healthy`, `summary`, `findings` (the flagged objects)
and `metrics` (the numbers and thresholds behind the verdict). Filter on
`healthy` rather than parsing prose:

```bash
pg-debug --json health | jq 'to_entries | map(select(.value.healthy == false))'
```

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

### When tuning reports fewer queries than expected

The workload comes from `pg_stat_statements` and not every entry can be used.
Entries that cannot be parsed or planned are skipped and the run says how many
and why — relay that rather than presenting the result as a complete picture.
The usual cause is privilege: a role that is neither a superuser nor a member
of `pg_read_all_stats` sees `<insufficient privilege>` instead of other roles'
query text, so the workload it analyzes is only its own queries. `GRANT
pg_read_all_stats TO <role>` fixes it. Queries against views are skipped by
design: an index goes on the tables underneath.

Each recommendation also carries a `write_cost` when the table has recorded
writes: how many inserts, deletes and non-HOT updates every index on it has to
be maintained for. Report it alongside the benefit rather than after it — an
index that halves a query and is maintained on ten million writes a day is a
different proposition from the same index on a table nobody writes to. A
`hot_update_warning` means most updates to that table currently avoid the
indexes entirely, and an index over a column they change would end that.

## Reviewing the server's configuration

```bash
pg-debug config              # settings judged against what this server is doing
pg-debug --json config
pg-debug config --exit-code  # exit 3 on a durability setting that is off
```

Every finding is argued from a counter, and the `evidence` field carries it —
quote that rather than the recommendation on its own, because it is what makes
the suggestion checkable. An empty result means nothing *measurable* is wrong,
which is not the same as optimal; say it that way.

It deliberately does not guess at hardware, so where the right value depends on
the machine's memory or storage the finding offers no number and says why. Do
not fill that gap with a number of your own — suggest measuring again after a
change instead.

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
`pg_partman` for creating and rolling partitions afterwards.

If nothing is recommended, that is a finding, not a failure to find one.

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

Facts worth knowing when reading its output, all measured rather than
recalled: `ADD COLUMN ... NOT NULL DEFAULT 0` does *not* rewrite the table on
PostgreSQL 11+; `ADD FOREIGN KEY` takes SHARE ROW EXCLUSIVE and never blocks
readers; `CREATE INDEX CONCURRENTLY` blocks nothing but cannot run inside a
transaction, so it cannot sit in a migration applied atomically. A default of
`now()` is stable and costs nothing; `clock_timestamp()` and `random()` are
volatile and force a rewrite — the tool resolves which through `pg_proc` when
connected.

## Reviewing the data model

Everything else here asks what the server is doing. This asks whether the
schema is built the way the data behaves.

```bash
pg-debug model                           # every rule, every user schema
pg-debug model --schema public
pg-debug model --rule unindexed_foreign_key
pg-debug model --proven-only             # drop what was inferred from statistics
pg-debug --json model                    # structured, with the DDL to apply
pg-debug model --exit-code               # exit 3 on a high-severity finding (CI)
```

Rules: `unindexed_foreign_key`, `foreign_key_type_mismatch`,
`missing_primary_key`, `column_order_padding`, `naive_timestamp`,
`blank_padded_char`, `nullable_never_null`, `low_cardinality_text`, `all`.
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

An empty result means the model has nothing wrong with it that these rules can
see — say that, rather than reaching for something to report.

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

## Exploring a schema

```bash
pg-debug schemas
pg-debug objects public                    # tables; -t view|sequence|extension
pg-debug describe public.orders            # columns, constraints, indexes
pg-debug describe orders --schema reporting
```

`describe` is the fastest way to check what indexes already exist before
recommending more.

## Scripting it

Add `--json` and the output is machine-readable. Everything except `explain`
returns rows or an object; `explain --json` returns the raw plan document.

```bash
pg-debug --json objects public | jq -r '.[].name'
pg-debug --json explain - < slow.sql | jq '.Plan."Total Cost"'
```

Exit status: `0` success, `1` error, `2` usage error, `3` for
`health --exit-code` when a check is unhealthy.

## From Python

Async is the native API; `SyncPgDebug` mirrors it exactly for code that is not.

```python
import asyncio
from pg_debug import AccessMode, PgDebug

async def main():
    async with PgDebug("postgresql://localhost/mydb", access_mode=AccessMode.RESTRICTED) as pg:
        plan = await pg.explain_query(
            "SELECT * FROM orders WHERE user_id = $1",
            hypothetical_indexes=[{"table": "orders", "columns": ["user_id"]}],
        )
        print(plan.plan_tree.total_cost)   # parsed nodes
        print(plan.to_text())              # readable summary

        for name, check in (await pg.analyze_db_health("index")).items():
            if not check.healthy:
                print(name, check.findings)

        for finding in await pg.review_data_model(schema="public"):
            print(finding.severity.value, finding.summary)
            if finding.confidence.value == "candidate":
                print("  confirm first:", finding.verify)
            print("  fix:", *finding.remediation, sep="\n    ")

asyncio.run(main())
```

```python
from pg_debug import SyncPgDebug

with SyncPgDebug("postgresql://localhost/mydb") as pg:
    rows = pg.execute_sql("SELECT count(*) FROM orders")
```

The client connects lazily; `await pg.connect()` first if you want a bad URL to
fail immediately. Close it (or use the context manager) so the pool is
released.

Failures raise rather than returning error strings. Catch `PgDebugError`, or
the specific subclass — `ConnectionFailedError`, `InvalidArgumentError`,
`ExtensionRequiredError` (has `.extension`), `ExplainPlanError`,
`MissingDependencyError`. The one exception is index tuning, which reports
problems as an `error` key in its result dict.

Returns: `list[dict]` for schemas/objects/rows/top-queries/activity/vacuum
progress/column statistics, `dict` for `get_object_details` and the tuning
results, `ExplainPlanArtifact` for plans, `dict[str, HealthCheckResult]` for
health, and `list[ModelFinding]` for the data model review.

The live-activity and statistics methods mirror the commands:
`list_activity(include_idle=, min_duration_seconds=, blocked_only=)`,
`lock_tree()`, `vacuum_progress()`, `column_stats(table)`, `extended_statistics(table)`, and
`get_top_queries(..., interval_seconds=)` for sampling. The model review is
`review_data_model(rules=, schema=, min_rows=, proven_only=)`; each
`ModelFinding` has `rule`, `confidence`, `severity`, `summary`, `detail`,
`evidence`, `remediation`, `verify` and `impact_bytes`, plus `to_dict()`.

Each `execute_sql` call may land on a different pooled connection, so session
state does not carry over: a `SET`, a temp table, an advisory lock or a
hypothetical index created in one call is not guaranteed to exist in the next,
and a `BEGIN` will not hold. When you need several statements in one session,
drop to the driver and pin it:

```python
driver = await pg.sql_driver()
async with driver.session():
    await driver.execute_query("SET LOCAL work_mem = '256MB'")
    rows = await driver.execute_query("SELECT ...")
```

## The LLM tuning method

`--method llm` asks a language model for candidate index sets instead of
enumerating them. Every suggestion is still scored with HypoPG against the real
planner, so the model proposes and the planner decides.

```bash
pip install 'pg-debug[llm]'
export OPENAI_API_KEY=sk-...
pg-debug tune-queries --method llm "SELECT * FROM orders WHERE user_id = 42"
```

Reach for it only when `dta` finds nothing and you suspect a non-obvious index.
It handles **one query at a time**, makes several paid API calls per run, and
samples at a high temperature, so runs differ. Set the model with
`--llm-model`, `PG_DEBUG_LLM_MODEL`, or `llm_model=` on the client (default
`gpt-4o`). For everyday tuning `dta` is faster, free and
reproducible — do not reach for `llm` by default, and tell the user it costs
money before running it.

## Answering well

The output of these commands is evidence, not a conclusion. A useful answer
names the specific finding, the number behind it, and what to do — "the plan
switches from a Seq Scan costing 448 to an Index Scan costing 14.8, so
`orders(user_id, status)` is worth its 68 kB", not "you should add an index".

When a number is missing or an extension is not installed, say so plainly
instead of estimating around it.
