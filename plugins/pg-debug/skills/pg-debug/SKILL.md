---
name: pg-debug
description: Diagnose and tune a live PostgreSQL database with the pg-debug CLI or its pg_debug Python library: why a query is slow, spilled to disk or misestimated its rows, whether an index would help (simulated with HypoPG, never created) and which to add, what is blocking a stuck database, health (bloat, unused indexes, vacuum and wraparound, stale statistics), the most expensive queries, server settings, data model defects, whether a migration will lock or rewrite a table, whether a table is worth partitioning, what sessions are waiting on over time, whether a major-version upgrade will go through, how the server's authentication and privileges stand, what a server log says happened (deadlocks, lock waits, temp files, autovacuum), gating CI on new findings or a changed query plan, and what tables, columns and indexes exist. It also covers running it behind a connection pooler (PgBouncer, Supavisor, transaction pooling) or on a managed or serverless platform (Neon, Supabase, RDS, Aurora, Cloud SQL, AlloyDB, Azure, scale-to-zero computes), read replicas, standbys and replication slots, and whether to shard (Citus). Use it whenever the user asks why Postgres is slow, stuck or blocked, about EXPLAIN plans, indexes, row-level security, schema design, migration safety, partitioning or partition maintenance, poolers, managed Postgres, replicas, sharding, upgrades, database security, server logs, CI checks or database health, even without naming pg-debug, and when writing code against pg_debug.
---

# pg_debug

`pg_debug` answers questions about a live PostgreSQL database: what is in it,
why a query is slow, whether an index would help, and whether anything is
quietly rotting. It is a Python library (`PgDebug`, async; `SyncPgDebug`,
blocking) with a `pg-debug` CLI on top.

## Getting the CLI

Check whether it is installed before anything else:

```bash
pg-debug --version
```

If it is not, run it with uv straight from its repository, which installs
nothing into the user's environment — uv builds it once and caches it:

```bash
uvx --from git+https://github.com/danplischke/pg_debug pg-debug --version
```

Then use that `uvx --from ... pg-debug` prefix wherever the examples say
`pg-debug`. Ask before installing it anywhere permanent; `uv tool install`
and `pipx install` take the same `git+https://...` source. If uv is missing
too, say what is missing rather than installing a toolchain unasked.

## Connecting

Every command needs a connection URL. Prefer the environment variable so the
password stays out of shell history and process listings:

```bash
export DATABASE_URI=postgresql://user:pass@host:5432/dbname
```

`-d/--database-url` overrides it. `$DATABASE_URL` works as a fallback.

If a command fails with `invalid byte sequence for encoding "UTF8"` and the
error says the database is `SQL_ASCII`, the database holds bytes in another
encoding. Add that encoding to the URL (`?client_encoding=LATIN1`) rather than
working around it.

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

**Behind a transaction pooler** (PgBouncer in transaction mode, Supavisor, a
`-pooler` host), check the build first. One whose `pg-debug --help` describes
`--search-path` as "for every statement" keeps its state inside each
transaction and works through the pooler. One that says "to set on the
connection", the released 0.2.0 among them, fails longer commands with
`prepared statement "_pg3_0" already exists` and leaves its timeout, search
path and hypothetical indexes on the pooler's backends for other clients: with
it, connect to the direct or session endpoint.

Connect as a dedicated role holding `pg_monitor` where the platform lets you
grant it, rather than the application's login role or the platform's admin
role; pg-debug needs no superuser. Planning a query also needs SELECT on its
tables, and `--role` needs the connecting role to be a member of that role.
Without `pg_read_all_stats`, which `pg_monitor` includes, other roles'
sessions and query text are hidden; `activity`, `locks`, `vacuum-progress`
and `top-queries` say on stderr what was withheld and the grant that would
show it. On PostgreSQL 15 and later a member of `pg_monitor` that does not
inherit it is hidden from too.

On a managed platform, identify it before relaying a finding. pg-debug
recognises the platform from the URL's host and the server's own settings and
roles, and uses it for three things only: the `fsync = off` and
`full_page_writes = off` Neon and Lakebase run with (low notes there, when the
`neon.timeline_id` setting, a `neon.tech` host or a Lakebase host says so), the advice on getting HypoPG, and keeping the timeout on its own side
on an Aurora DSQL host, which refuses `statement_timeout`. A Neon suspend, a
crash, a failover or a point-in-time restore starts the counters again from
zero; `health`, `config` and `top-queries` say since when theirs count, and
the unused-index check declines to judge on less than a week.
Connecting also wakes a scale-to-zero compute (Neon, Aurora serverless), and
its first timings run cold. [platforms.md](references/platforms.md) has the
endpoints, roles and cautions for each platform.

## Which command answers which question

Run the command, then read the reference file next to this one before
relaying what it found. Each says how to read that output, what it needs, and
which conclusions it does not support — the difference between quoting a tool
and answering the question.

| The question | Start with | Then read |
| --- | --- | --- |
| Why is this query slow? | `pg-debug diagnose "<query>"`, or `diagnose --plan plan.json` for a plan the user already has | [slow-queries.md](references/slow-queries.md) |
| Would an index help? Which indexes should I add? | `pg-debug explain "<query>" -i 'table(column)'`, `tune-queries`, `tune-workload` | [slow-queries.md](references/slow-queries.md) |
| Why is my index not used? | `pg-debug index-use "<query>"` | [slow-queries.md](references/slow-queries.md) |
| Why are the row estimates wrong? | `pg-debug column-stats <table>`, `health --type statistics` | [slow-queries.md](references/slow-queries.md) |
| Why does the application see other rows or plans? (row-level security, a role, session variables) | `pg-debug explain --role ROLE --set NAME=VALUE "<query>"`, or the project's `pg-debug.toml` | [slow-queries.md](references/slow-queries.md) |
| The database is stuck. Who is blocking whom? | `pg-debug activity --blocked`, `locks`, `vacuum-progress` | [incidents.md](references/incidents.md) |
| What are sessions waiting on? (no pg_wait_sampling needed) | `pg-debug activity --sample 10` | [incidents.md](references/incidents.md) |
| Is the database healthy? | `pg-debug health` | [health.md](references/health.md) |
| Could an index or a page be corrupt? (an OS or ICU upgrade, checksum failures) | `pg-debug health --type collation,checksum` | [incidents.md](references/incidents.md) |
| What is the database spending its time on? | `pg-debug top-queries --interval 10` | [health.md](references/health.md) |
| What is the server doing per second right now? (commits, I/O, temp files, WAL, checkpoints, slot growth) | `pg-debug server-rates --interval 10` | [health.md](references/health.md) |
| Are clients waiting in PgBouncer? Is the pool too small? | `pg-debug pooler postgresql://user@host:6432/pgbouncer` | [platforms.md](references/platforms.md) |
| Are the server's settings right? | `pg-debug config` | [health.md](references/health.md) |
| Is the schema built well? | `pg-debug model` | [schema.md](references/schema.md) |
| Is this migration safe to run? | `pg-debug check-migration <file>` | [schema.md](references/schema.md) |
| Can we upgrade to the next major version? Is the server secure? | `pg-debug upgrade-check --to 18`, `pg-debug security` | [schema.md](references/schema.md) |
| Put a check in CI: gate a build, suppress a rule, `--static` on an empty database, fail when a plan changes | `pg-debug model --static --exit-code`, `health --exit-code --baseline b.json`, `plans --queries q.sql --baseline p.json --exit-code` | [rules.md](references/rules.md) |
| What happened, from the server log? (deadlocks, lock waits, temp files, skipped or slow autovacuum, checkpoints) | `pg-debug logs postgresql.log` — no connection | [rules.md](references/rules.md) |
| Should this table be partitioned? | `pg-debug partitions` | [schema.md](references/schema.md) |
| Partition maintenance: pruning, attach and detach, pg_partman | `pg-debug health --type partition`; `diagnose "<query>"` against the partitioned table; `check-migration` for the partition DDL | [schema.md](references/schema.md) |
| What tables, columns and indexes exist? | `pg-debug schemas`, `objects`, `describe` | below |
| Running it behind a connection pooler (PgBouncer, Supavisor, a `-pooler` host) | `pg-debug --version` and `--help`, as Connecting says; a build that sets `--search-path` "on the connection" needs the direct or session endpoint | [platforms.md](references/platforms.md) |
| On a managed or serverless platform (Neon, Supabase, RDS/Aurora, Cloud SQL/AlloyDB, Azure) | the connection URL's host and port, to name the platform, then the command for the question | [platforms.md](references/platforms.md) |
| On a read replica, or about replication slots | `pg-debug health --type replication,vacuum`; the replica's own URL for the queries it runs | [scale-out.md](references/scale-out.md) |
| Should we shard? (Citus, sharding proxies) | `pg-debug shard-readiness --interval 30` | [scale-out.md](references/scale-out.md) |
| What history does the platform keep? (Azure Query Store, Aurora plans, the Cloud SQL/AlloyDB index advisor, Neon's file cache, pg_qualstats, pg_wait_sampling) | `pg-debug history` | [platforms.md](references/platforms.md) |
| Writing Python that uses pg_debug | the `pg_debug` package | [python.md](references/python.md) |

An empty result is an answer, not a failure: a plan whose estimates matched, a
model with nothing wrong, a table not worth partitioning. Say so, rather than
reaching for something else to report.

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

Exit status: `0` success, `1` error, `2` usage error, `3` when an
`--exit-code` gate (`health`, `config`, `model`, `upgrade-check`, `security`, `check-migration`, `plans`) found what
it gates on. In JSON every finding of `health`, `model`, `config` and `logs`
carries a `rule` id and a `docs` link; [rules.md](references/rules.md) lists them. `health` also exits `1` when a check could not run, unless its
gate found a problem among the checks that did.

## Answering well

The output of these commands is evidence, not a conclusion. A useful answer
names the specific finding, the number behind it, and what to do — "the plan
switches from a Seq Scan costing 448 to an Index Scan costing 14.8, so
`orders(user_id, status)` is worth its 68 kB", not "you should add an index".

When a number is missing or an extension is not installed, say so plainly
instead of estimating around it.
