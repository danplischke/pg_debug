---
name: pg-debug
description: Diagnose and tune a live PostgreSQL database with the pg-debug CLI or its pg_debug Python library: why a query is slow, spilled to disk or misestimated its rows, whether an index would help (simulated with HypoPG, never created) and which to add, what is blocking a stuck database, health (bloat, unused indexes, vacuum and wraparound, stale statistics), the most expensive queries, server settings, data model defects, whether a migration will lock or rewrite a table, whether a table is worth partitioning, and what tables, columns and indexes exist. Use it whenever the user asks why Postgres is slow, stuck or blocked, about EXPLAIN plans, indexes, schema design, migration safety, partitioning or database health, even without naming pg-debug, and when writing code against pg_debug.
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
| The database is stuck. Who is blocking whom? | `pg-debug activity --blocked`, `locks`, `vacuum-progress` | [incidents.md](references/incidents.md) |
| Is the database healthy? | `pg-debug health` | [health.md](references/health.md) |
| What is the database spending its time on? | `pg-debug top-queries --interval 10` | [health.md](references/health.md) |
| Are the server's settings right? | `pg-debug config` | [health.md](references/health.md) |
| Is the schema built well? | `pg-debug model` | [schema.md](references/schema.md) |
| Is this migration safe to run? | `pg-debug check-migration <file>` | [schema.md](references/schema.md) |
| Should this table be partitioned? | `pg-debug partitions` | [schema.md](references/schema.md) |
| What tables, columns and indexes exist? | `pg-debug schemas`, `objects`, `describe` | below |
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
`--exit-code` gate (`health`, `config`, `model`, `check-migration`) found what
it gates on.

## Answering well

The output of these commands is evidence, not a conclusion. A useful answer
names the specific finding, the number behind it, and what to do — "the plan
switches from a Seq Scan costing 448 to an Index Scan costing 14.8, so
`orders(user_id, status)` is worth its 68 kB", not "you should add an index".

When a number is missing or an extension is not installed, say so plainly
instead of estimating around it.
