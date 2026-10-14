# pg_debug

[![CI](https://github.com/danplischke/pg_debug/actions/workflows/ci.yml/badge.svg)](https://github.com/danplischke/pg_debug/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pg-debug)](https://pypi.org/project/pg-debug/)
[![Python](https://img.shields.io/pypi/pyversions/pg-debug)](https://pypi.org/project/pg-debug/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

PostgreSQL inspection, health checks and index tuning — as an async Python library with a CLI.

`pg_debug` is an extraction of the analysis engine behind
[crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp). The database
work is the same; the MCP server layer has been replaced by a plain Python API and a
`pg-debug` command, so the tooling can be used from scripts, tests, CI and the shell
without an MCP client in the loop.

## What it does

- **Schema introspection** — list schemas, tables, views, sequences and extensions; describe an object's columns, constraints and indexes.
- **Explain plans** — plan or `EXPLAIN ANALYZE` a query, including plans against *hypothetical* indexes that are never created (via [HypoPG](https://github.com/HypoPG/hypopg)).
- **Index use** — why the planner did not use an index for a query: the query restricts a different column, or the column wrapped in an expression the index is not on, an operator a btree cannot search, a cast the plan applies to the column, a partial index the query does not match, an invalid index — or nothing stops it and it judged another plan cheaper, in which case the finding carries both costs and the row estimate the choice rests on.
- **Plan diagnosis** — read the plan rather than only print it: the node whose row estimate first diverged from what it found, and whether that is stale statistics or two correlated columns nothing has told the planner about; sorts and hashes that outgrew `work_mem`; scans reading rows in order to discard them; parallel workers asked for and not granted. Each finding is ranked by the share of the query's work behind it.
- **Live activity** — what every backend is doing right now, what it is waiting on, and which process is blocking it.
- **Lock tree** — who is blocking whom, as a tree rooted at the session that waits on nothing: what it is doing, what every backend beneath it waits for, and the mode the holder has.
- **Vacuum progress** — running vacuums, their phase and how far through the heap they are.
- **Planner statistics** — the per-column numbers behind a plan's row estimates, the extended statistics defined on a table, and which tables the planner is reasoning about with stale numbers.
- **Configuration review** — the server's settings judged against what it is actually doing: `work_mem` against bytes spilled to temporary files, `shared_buffers` against the cache hit rate it is achieving, `max_wal_size` against how many checkpoints were forced rather than scheduled. A default that is doing no harm is left alone, and nothing here guesses at the machine's memory or storage.
- **Migration review** — read a migration before running it and see, per statement, the lock it takes, how long it holds it, whether it rewrites the table, and a safer rewrite where one exists. Connected, it reports how big the tables actually are, which is what decides whether a rewrite is a pause or an outage.
- **Data model review** — foreign keys with no index behind them, keys whose two sides are different types, tables with no primary key, columns ordered so alignment padding costs bytes on every row, and types that do not mean what they look like. Each finding says whether it is proven from the catalog or inferred from statistics, and carries the DDL that fixes it.
- **Partition advice** — whether a table is worth partitioning, decided by the share of recorded query time that would still prune rather than by the table's size. It is built to say no: a 400 GB table where a third of the query time filters on a timestamp is a worse table after partitioning, and the verdict comes with the number behind it either way.
- **Health checks** — invalid/duplicate/bloated/unused indexes, table bloat, connection utilization, transaction ID wraparound risk, autovacuum backlog and what is holding the vacuum horizon, sequences near their limit, replication lag and slots, buffer cache hit rates, invalid constraints, statistics freshness, tables read sequentially far more than through an index.
- **Top queries** — the most expensive queries recorded by `pg_stat_statements`, ranked by resource blend or by execution time, cumulatively or sampled over an interval.
- **Index tuning** — recommend indexes for your recorded workload or for a specific set of queries, with before/after planner costs, the size HypoPG measures for each candidate, and what each one will cost to keep up to date.
- **Access modes** — run unrestricted, or in a `restricted` mode that parses each statement, rejects anything that is not read-only, and runs it in a read-only transaction with a statement timeout.

## Install

```bash
pip install pg-debug            # library + CLI
pip install 'pg-debug[llm]'     # adds the optional LLM index tuning method
```

Requires Python 3.12+ and PostgreSQL 12+.

Some features need server-side extensions: index tuning and hypothetical-index plans
need `hypopg`, and workload analysis and `top-queries` need `pg_stat_statements`. The
library tells you which one is missing and how to install it.

No superuser is required. Everything runs through HypoPG's functions, which are
executable by any role; the extension's convenience *views* are not readable by
default, and pg_debug does not use them.

## CLI

```bash
export DATABASE_URI=postgresql://user:pass@localhost:5432/mydb   # or pass -d/--database-url

pg-debug schemas
pg-debug objects public --type table
pg-debug describe public.orders

pg-debug execute "SELECT count(*) FROM orders"
pg-debug explain "SELECT * FROM orders WHERE user_id = 42"
pg-debug explain "SELECT * FROM orders WHERE user_id = 42" -i 'orders(user_id)'
pg-debug explain --analyze "SELECT * FROM orders LIMIT 10"

pg-debug diagnose "SELECT * FROM orders WHERE status = 'open' AND region = 'eu'"
pg-debug diagnose ./slow.sql            # what is wrong with this plan, and why
pg-debug --json diagnose ./slow.sql | jq '.[] | select(.severity == "high")'
pg-debug diagnose --plan plan.json       # a plan you already have; no query is run
pg-debug diagnose --plan - < auto_explain.json   # and no database needed

pg-debug index-use "SELECT * FROM orders WHERE lower(email) = 'a@b.c'"   # why is my index not used?
pg-debug index-use -i orders_email_idx ./query.sql

pg-debug explain --role app_user --set app.tenant_id=7 "SELECT * FROM orders WHERE total > 100"   # as the app sees it
pg-debug diagnose --set work_mem=64MB ./slow.sql   # try the work_mem diagnose suggested

pg-debug activity                       # what is running right now
pg-debug activity --blocked              # only backends waiting on a lock
pg-debug activity --min-duration 30      # queries running longer than 30s
pg-debug locks                           # who is blocking whom, root blockers first
pg-debug vacuum-progress

pg-debug health
pg-debug health --type index,vacuum
pg-debug column-stats orders             # why the planner estimates what it does

pg-debug config                          # settings vs what the server is doing
pg-debug config --exit-code              # fails on a durability setting that is off

pg-debug partitions                      # is any table worth partitioning?
pg-debug partitions --min-size 500MB --min-pruning-share 0.7

pg-debug check-migration 001_add_column.sql   # what will this lock, and for how long?
pg-debug check-migration --offline --exit-code migrations/*.sql

pg-debug model                           # review the schema itself
pg-debug model --schema public --proven-only
pg-debug --json model | jq '.[] | select(.severity == "high")'

pg-debug top-queries --sort-by mean_time --limit 5
pg-debug top-queries --interval 10       # measure the next 10 seconds instead

pg-debug tune-queries "SELECT * FROM orders WHERE user_id = 42"
pg-debug tune-workload --max-index-size-mb 500
```

### Global options

These go before the command (`pg-debug --json health`, not `pg-debug health --json`):

| Option | Meaning |
| --- | --- |
| `-d, --database-url URL` | Connection URL. Defaults to `$DATABASE_URI`, then `$DATABASE_URL`. |
| `--access-mode {unrestricted,restricted}` | `unrestricted` (default) runs any statement; `restricted` allows read-only statements only. |
| `--timeout SECONDS` | Statement timeout. Defaults to none in unrestricted mode and 30 in restricted mode. |
| `--search-path SCHEMAS` | Schema search path for the connection, e.g. `reporting,public`. |
| `--llm-model MODEL` | Model for the `llm` tuning method. Defaults to `$PG_DEBUG_LLM_MODEL`, then `gpt-4o`. |
| `--json` | Emit raw JSON instead of formatted text. |
| `-v, --verbose` | Log at INFO, or DEBUG when repeated. |
| `--version` | Print the version and exit. |

### Commands

| Command | Arguments and options |
| --- | --- |
| `schemas` | — |
| `objects SCHEMA` | `-t, --type {table,view,sequence,extension}` (default `table`) |
| `describe NAME` | `-s, --schema SCHEMA` (default `public`, ignored if `NAME` is qualified), `-t, --type {table,view,sequence,extension}` |
| `execute SQL` | — |
| `explain SQL` | `--analyze`, `-i, --hypothetical-index SPEC` (repeatable) |
| `diagnose SQL` | a file path, inline SQL, or `-` for stdin; `--min-ratio N` (default 10); `--plan` to read `EXPLAIN (ANALYZE, FORMAT JSON)` output instead of running a query, `--offline` to not connect even with a URL set |
| `activity` | `-a, --all` (include idle), `-m, --min-duration SECONDS`, `-b, --blocked` |
| `locks` | — |
| `vacuum-progress` | — |
| `index-use SQL` | a file path, inline SQL, or `-` for stdin; `-i, --index NAME` to report one index |
| `column-stats TABLE` | — |
| `health` | `-t, --type CHECKS` (comma-separated, default `all`), `--exit-code`, `--baseline FILE`, `--save-baseline FILE` |
| `config` | `--exit-code`, `--baseline FILE`, `--save-baseline FILE` |
| `partitions` | `--min-size SIZE` (default 1GB, accepts `500MB`/`2GB`), `--min-pruning-share SHARE` (default 0.5), `--limit N` (default 200) |
| `check-migration SQL` | a file path, inline SQL, or `-` for stdin; `--offline`, `--exit-code` |
| `model` | `-s, --schema SCHEMA`, `-r, --rule RULES` (comma-separated, default `all`), `--proven-only`, `--min-rows N` (default 1000), `--exit-code`, `--baseline FILE`, `--save-baseline FILE` |
| `top-queries` | `-s, --sort-by {resources,mean_time,total_time}` (default `resources`), `-n, --limit N` (default 10, ignored for cumulative `resources`), `-i, --interval SECONDS` |
| `tune-workload` | `--max-index-size-mb MB` (default 10000), `--method {dta,llm}` (default `dta`) |
| `tune-queries QUERY...` | up to 10 queries, same options as `tune-workload` |

`execute`, `explain`, `diagnose`, `index-use`, `tune-workload` and
`tune-queries` also take the [query context](#running-a-query-as-your-application-does)
options: `--role ROLE`, `--set NAME=VALUE` (repeatable), `--config FILE` and
`--no-config`.

Health checks are `index`, `connection`, `vacuum`, `sequence`, `replication`,
`buffer`, `constraint`, `statistics`, `table`, `scan` and `all`.

`health`, `model` and `config` can gate a build with `--exit-code`. The
trouble with adopting such a gate is the first run: an existing database has
findings, some of them deliberate, and a gate that fails on all of them on day
one is a gate that gets turned off. A baseline is the set of findings a team
has read and accepted:

```bash
pg-debug health --save-baseline health-baseline.json     # from a run you accept
pg-debug health --exit-code --baseline health-baseline.json   # fails only on what is new
```

A baseline records what each finding is about — the check and the object it
names, the rule and the table, the setting — and never the numbers, so a
bloated index is the same finding at 31% and at 34%. A finding that goes away
simply stops being listed. The file is JSON, one finding per line in a stable
order, meant to live in the repository next to the workflow that reads it. In
JSON output each finding carries `"baseline": "known"` or `"new"`.

`scan` reports tables read sequentially far more than through an index, from
`pg_stat_user_tables` — no extension needed. It argues in bytes rather than
scan counts: each table is charged the data its sequential scans read (scans
times size, scaled down when a scan returned only part of the table, which is
what a `LIMIT` that stopped early looks like), and reported only when that
reading reaches 1 GB, the table is at least 10 MB, and sequential scans
outnumber index scans. The queries behind the scans are not visible from the
counters; `top-queries` lists them and `tune-queries` says which index serves
them.

`table` estimates how much of each table's heap holds no live rows, from
`pg_stats` and `pg_class` — no extension needed. It builds a row the way
PostgreSQL does, padding each column forward to its type's alignment boundary
and honouring the table's `fillfactor`, so a table with nothing wrong estimates
exactly. A table is reported only when it wastes at least 100 MB *and* at least
20% of its size, so ordinary free space in a large table is not flagged. What
the estimate cannot see is the *spread* of a variable-width column, since
`pg_stats` records only an average — such a table reads a few percent bloated
when it is not. `pgstattuple` gives the exact figure at the cost of scanning
the table.

Index tuning weighs what an index costs to maintain, not only what it saves.
Every insert, every delete and every update that changes an indexed column has
to maintain each index on the table — but a *HOT* update, one that changes no
indexed column, updates no index at all, so those are subtracted rather than
counted. Each recommendation reports the writes it will be maintained for, and
warns when a table's updates are mostly HOT today, because an index over the
wrong column gives that up.

The same figure feeds the search's objective, at a deliberately small weight:
it breaks ties between candidates of similar benefit in favour of the quieter
table, and does not overrule a real improvement. `write_weight=0` restores the
previous behaviour of ignoring maintenance entirely.

`config` reviews the server's settings, but argues every finding from a counter
this server is already keeping rather than from a table of recommended values.
`work_mem` is only raised if queries have actually spilled to disk;
`shared_buffers` well below the database size is only mentioned if the cache is
actually missing; `max_wal_size` only if checkpoints are being forced rather
than scheduled. It does not guess at hardware — PostgreSQL cannot see the
machine's memory or whether its storage is solid state, so no finding rests on
assuming either, and where the right value depends on something invisible from
here the finding says so instead of inventing a number.

`partitions` decides from the workload, not from the table. Partitioning pays
when queries filter on the partition key — they read one partition instead of
all of them — and it pays most when old rows are removed by that key, because
dropping a partition replaces a DELETE and the vacuum after it. It costs when
queries *do not* filter on the key, since those now read every partition, and
it constrains the model: every unique constraint has to include the partition
key. So the number the verdict turns on is the share of recorded query time
that restricts on a candidate key, and a table that does not clear the
threshold is reported as examined-and-refused with that share attached, rather
than left out. Half the value of the command is the tables it talks you out of.

What comes out for a recommended table is the target shape, not a migration:
converting a populated table means creating the partitioned one, copying,
reconciling constraints and swapping names, and generating that unseen is how
people lose data. The sketch does run, though — where the primary key would
have to gain the partition key it is recreated rather than copied, because
`LIKE ... INCLUDING ALL` brings the old key along and PostgreSQL rejects the
CREATE outright. The monthly boundaries come from the column's histogram, so
they cover the span the data actually occupies.

`diagnose` reads the plan instead of only printing it. The number it turns on
is the row estimate: the planner picks a join method, a scan and an order from
how many rows it expects, so an estimate that is out by two orders of magnitude
is usually the reason a query is slow, and every plan above the mistake was
chosen from the wrong number. Only the *lowest* diverging node is reported —
the error propagates upwards, and listing every node it touched buries the
cause under its own consequences.

Where a bad estimate has a nameable cause, the finding says which. Statistics
that no longer describe the table point at `ANALYZE`; several columns of one
table restricted at once, with nothing in `pg_statistic_ext` covering them,
point at `CREATE STATISTICS` — the planner multiplies their selectivities as
though they were independent, which is exactly how an estimate comes out far
below the truth. The rest is read straight off the plan: a sort or a hash that
outgrew `work_mem`, a bitmap that went lossy, a scan reading rows to discard
them, workers planned for and not granted.

Findings are ranked by the share of the query's work behind them, and a bad
estimate is charged for the whole subtree it misled rather than for the scan
itself, which is often trivial. The shares overlap and are not a breakdown.

This needs `EXPLAIN ANALYZE`, which runs the query, so `restricted` mode
refuses outright rather than half-answering — its promise is that nothing
executes. A statement that writes is run in a transaction that is rolled back,
which undoes the rows but not a sequence it consumed or a trigger that reached
outside the database.

A plan that already exists can be read instead: `--plan` takes
`EXPLAIN (ANALYZE, FORMAT JSON)` output — a file, stdin, or the object
`auto_explain` logs with `auto_explain.log_format = 'json'` — and runs nothing,
so it works in `restricted` mode and, with no database URL at all, with no
connection. Connected, the tables the plan reads are checked for stale
statistics and for extended statistics covering their correlated columns, which
is why the plan should come from this database or one with the same schema and
data. Unconnected, those two questions go unanswered and a misestimate says so,
rather than letting the silence read as a clean bill. The text format EXPLAIN
prints by default is not read.

`index-use` answers "why is my index not used?", which has two halves that
need different evidence. Whether the planner *can* use the index is read from
the query's text against the index's definition and the catalog: which column
each predicate restricts, bare or wrapped in a function or cast (`lower(email)`
and `email` are different keys), with which operator (`<>`, `NOT IN`, a
leading-wildcard `LIKE`, `ILIKE` and regular expressions cannot be looked up
in a btree; `LIKE 'prefix%'` needs the C collation or `text_pattern_ops`),
whether the index is valid, whether a partial index's `WHERE` is matched, and
whether the plan casts the column before comparing it. Whether it *chose* to
is read by planning the query again with sequential scans disabled: if the
index is used then, nothing stopped it, and the finding reports both costs and
the share of the table the planner expects the predicate to match, which is
the number the choice turns on. If another index serves the query either way,
that index is named: the planner cannot be made to prefer one index over
another. Nothing is executed; the query is planned, not run. The forced plan
needs a `SET` for the session, which `restricted` mode does not allow, and the
verdict says it was skipped rather than guessing.

`check-migration` answers a different question from the rest: not what the
database is, but what a change will do to everyone using it while it runs. The
lock level alone decides little — ACCESS EXCLUSIVE held for a catalog update
lasts microseconds, while the same lock held for a rewrite is an outage — so
each statement reports the lock *and* how long it is held, and, when connected,
how big the table it is held on actually is.

The lock and rewrite behaviour it reports was measured against PostgreSQL 16
rather than taken from documentation: each statement was run in a transaction,
`pg_locks` read for the modes it held, and `pg_class.relfilenode` compared
before and after. Several results contradict widely repeated advice —
`ADD COLUMN ... NOT NULL DEFAULT 0` has not rewritten the table since
PostgreSQL 11, and `ADD FOREIGN KEY` takes SHARE ROW EXCLUSIVE, so readers are
never blocked. An integration test re-measures three of these against the
server it runs on, so a future PostgreSQL that changes one is caught rather
than believed.

`--offline` skips the connection. Severity is otherwise softened when the table
is small on the database you are pointed at, which is right for production and
misleading for a staging copy; in CI, prefer `--offline` unless the database
you can reach is representative.

The `vacuum` checks cover three things: transaction ID wraparound, tables
autovacuum is not keeping clean, and what is stopping vacuum from reclaiming
anything. The last is the one that explains bloat which keeps coming back — a
row deleted after the oldest transaction that might still need to see it cannot
be removed by any amount of vacuuming, so one forgotten transaction, one
replication slot nobody consumes or one prepared transaction pins the whole
database. It is narrower than the usual advice to hunt "idle in transaction":
measured on PostgreSQL 16, a READ COMMITTED transaction that has only *read*
holds neither a snapshot nor a transaction id between statements and blocks
nothing, so it is not reported. What is reported is a backend holding a
snapshot, or holding a transaction id because it has written.

`model` reviews the data model rather than the running server, and needs no
extension. Its rules are `unindexed_foreign_key`, `foreign_key_type_mismatch`,
`missing_primary_key`, `column_order_padding`, `naive_timestamp`,
`blank_padded_char`, `nullable_never_null`, `low_cardinality_text` and `all`.

Each finding carries a confidence. A **proven** finding is read from the
catalog and cannot be wrong: a foreign key either has an index behind it or it
does not. A **candidate** is inferred from sampled planner statistics — a
column with no NULLs in the sample may still have one — so it carries a
`verify` query that settles the question before any DDL is applied.
`--proven-only` drops the candidates.

Severity comes from the evidence, not from the rule: an unindexed foreign key
on a 4 GB child whose parent has recorded deletes is `high`, the same key on a
40-row lookup table is `low`. Findings are ordered by severity and then by each
rule's own measure of impact — deliberately not by a single score across rules,
since a byte count and a lock risk have no common scale.

`column_order_padding` reuses the tuple layout the `table` health check
measures with, so the two cannot disagree: a table it calls optimally packed is
one the bloat check will not accuse of padding. On a table of fixed-width
columns its prediction is exact, page for page.

Every statement pg_debug issues is prefixed with a `/* pg_debug */` comment,
in both access modes, and its own reports exclude anything carrying it —
`top-queries` and index tuning report the workload, not the tool looking at it.
This applies to statements you run *through* pg_debug as well: a query sent
with `pg-debug execute` or `explain` is the operator poking at the database,
not the application, and it is excluded on the same grounds.

The marker identifies a statement rather than partitioning one.
`pg_stat_statements` groups by a hash of the parsed statement, and comments are
no part of that hash, so a query pg_debug and your application both run shares
a single entry whose recorded text is whichever of the two ran first. In
practice this only reaches queries as generic as `SELECT 1`; pg_debug's
catalog reads are not ones an application issues.

Index tuning reads its workload from `pg_stat_statements`, where not every
entry is usable: a role that is neither a superuser nor a member of
`pg_read_all_stats` sees `<insufficient privilege>` in place of other roles'
query text, and a query recorded without its constants may not plan once
values are substituted back in. Those entries are skipped and the run reports
how many and why. Queries against views are skipped too — an index belongs on
the tables underneath a view, and HypoPG cannot put one on the view itself.

A hypothetical index `SPEC` is `table(column, ...)` with an optional method —
`orders(user_id)`, `orders(user_id, created_at):btree`,
`reporting.orders(status):hash`. Columns may be expressions, as in
`CREATE INDEX`: `users(lower(email))`,
`orders(date_trunc('day', created_at))`,
`users((first_name || ' ' || last_name))`. Commas inside a function call or a
quoted string belong to that expression, not to the column list.

### Running a query as your application does

pg-debug connects as whoever the URL names, often the owner of the tables, and
your application probably does not. It connects as a role of its own and sets
variables for its session, and the planner sees all of that. Run as the owner,
a query on a table with row-level security sees every tenant's rows, because
owners bypass the policies, and plans without the policy's predicate: a
`Seq Scan` where the application gets an index scan on `tenant_id`.
`EXPLAIN ANALYZE` of a query whose policy reads a variable nobody set fails
outright.

A query context declares the role and settings your application runs with.
Put it in a `pg-debug.toml`, which a command reads from the directory it runs
in or the nearest parent that has one:

```toml
[context]
role = "app_user"

[context.settings]
app.tenant_id = "7"
work_mem = "64MB"
```

or give it on the command line, where `--role` and `--set` override the file:

```bash
pg-debug explain --role app_user --set app.tenant_id=7 "SELECT * FROM orders WHERE total > 100"
pg-debug diagnose --set work_mem=256MB ./slow.sql    # the setting diagnose suggested, tried before it is set
```

The context applies to the statements that run or plan your query (`execute`,
`explain`, `diagnose`, `index-use` and the plans the tuners compare) and to
nothing else. It is applied inside each statement's own transaction, with
`set_config(name, value, true)` and the role last, as `SET LOCAL ROLE` would:
it ends with the transaction, so it never outlives the statement on a pooled
connection. pg-debug's own reads of the catalog run as the connecting user, who
sees statistics that row-level security hides from the application's role.
Commands that only read the catalog (`health`, `config`, `top-queries` and the
rest) do not take a context and do not read the file.

Each command that applies one says so on stderr, with where it came from:

```text
pg-debug: query context: role app_user, app.tenant_id=7, work_mem=64MB (from /work/shop/pg-debug.toml)
```

pg-debug checks the context against the server before running anything: a role
the connecting user is not a member of, or a setting the server does not know,
fails once, with PostgreSQL's reason. `--config FILE` reads another file and
`--no-config` reads none.

- A setting's name may be quoted, `"app.tenant_id" = "7"`, or written as a
  dotted key, as above. Values are text, as `set_config` takes them; a number
  or a boolean is turned into text, `true` into `"true"`. A JSON value, such as
  PostgREST's `request.jwt.claims`, is a string.
- `statement_timeout`, `search_path`, the role and the read-only settings are
  refused, each with the option that owns it (`--timeout`, `--search-path`,
  `role`, `--access-mode`). A context cannot loosen restricted mode, and it
  works there: setting a role or a variable writes nothing.
- The file holds the context and nothing else. The connection URL, and with it
  the password, never goes in it.
- Switching to a role does not apply that role's own defaults, the ones
  `ALTER ROLE ... SET` gives it at login. If your application's role has any,
  declare them under `[context.settings]` too.
- The index advisor takes its candidate columns from the query's text. Under a
  context it costs every candidate with the policy's predicate in the plan, but
  it does not propose an index on a column only the policy restricts;
  `explain -i 'orders(tenant_id, total)'` tests one. `tune-workload` plans every
  recorded query as the context's role, so queries that role may not run are
  skipped, with PostgreSQL's reason for the first.

SQL arguments accept `-` to read the statement from stdin:

```bash
pg-debug --json explain - < slow_query.sql | jq '.Plan."Total Cost"'
```

`health` and `model` can gate a CI job on the result:

```bash
pg-debug health --type index,vacuum --exit-code
pg-debug model --exit-code               # fails only on a high-severity finding
```

Exit status is `0` on success, `1` on error, `2` on a usage error, and `3` for
`health --exit-code` when a check reports a problem or `model --exit-code` when
a high-severity finding is reported.

`python -m pg_debug` works the same as the `pg-debug` command.

## Library

Everything is async and returns plain Python data structures.

```python
import asyncio
from pg_debug import AccessMode, PgDebug

async def main():
    async with PgDebug("postgresql://localhost/mydb", access_mode=AccessMode.RESTRICTED) as pg:
        for schema in await pg.list_schemas():
            print(schema["schema_name"], schema["schema_type"])

        plan = await pg.explain_query(
            "SELECT * FROM orders WHERE user_id = $1",
            hypothetical_indexes=[{"table": "orders", "columns": ["user_id"]}],
        )
        print(plan.to_text())          # readable summary
        print(plan.plan_tree.total_cost)  # parsed plan nodes

        for name, check in (await pg.analyze_db_health("index,vacuum")).items():
            print(f"{name}: {check.summary}")
            for finding in check.findings:      # the objects it flagged
                print("   ", finding)

        result = await pg.analyze_query_indexes(["SELECT * FROM orders WHERE user_id = 42"])
        for rec in result["recommendations"]:
            print(rec["index_definition"], rec["index_estimated_size"])

asyncio.run(main())
```

The client connects lazily on first use; `await pg.connect()` up front fails fast on a
bad URL instead.

A [query context](#running-a-query-as-your-application-does) is a
`QueryContext`, applied by `execute_sql`, `explain_query`, `diagnose_query`,
`explain_index_use` and index tuning:

```python
from pg_debug import PgDebug, QueryContext

context = QueryContext(role="app_user", settings={"app.tenant_id": "7"})
async with PgDebug("postgresql://localhost/mydb", context=context) as pg:
    plan = await pg.explain_query("SELECT * FROM orders WHERE total > 100")

# or the one the command line uses
context = QueryContext.from_file("pg-debug.toml")
```

### Return values

| Method | Returns |
| --- | --- |
| `list_schemas()` | `list[dict]` — one per schema |
| `list_objects(schema, object_type="table")` | `list[dict]` — one per object |
| `get_object_details(schema, name, object_type="table")` | `dict` with `basic`, `columns`, `constraints`, `indexes` |
| `execute_sql(sql)` | `list[dict]` — one per row, empty for statements with no result set |
| `explain_query(sql, analyze=False, hypothetical_indexes=None)` | `ExplainPlanArtifact` |
| `diagnose_query(sql, min_ratio=10)` | `list[PlanFinding]` — most severe first |
| `diagnose_plan(plan, min_ratio=10)` | `list[PlanFinding]` — the same, over `EXPLAIN (ANALYZE, FORMAT JSON)` output; works in restricted mode. `pg_debug.diagnose_plan(plan)` does it with no database at all |
| `list_activity(include_idle=False, min_duration_seconds=None, blocked_only=False)` | `list[dict]` — one per backend, with `blocked_by` |
| `lock_tree()` | `list[dict]` — one per root blocker, waiters nested under `blocking` |
| `vacuum_progress()` | `list[dict]` — one per running vacuum |
| `explain_index_use(sql, index=None)` | `list[IndexUse]` — one per index on the query's tables, unused first, each with `findings` |
| `column_stats(table)` / `extended_statistics(table)` | `list[dict]` — planner statistics |
| `analyze_db_health(health_type="all")` | `dict[str, HealthCheckResult]` — check name to result |
| `review_data_model(rules="all", schema=None, min_rows=1000, proven_only=False)` | `list[ModelFinding]` — most severe first |
| `check_migration(sql)` | `list[StatementReview]` — most severe first |
| `review_configuration()` | `list[ConfigFinding]` — most severe first |
| `suggest_partitions(sql=None, min_table_bytes=1GB, min_pruning_share=0.5, limit=200)` | `list[PartitionCandidate]` — recommended first, then by size |
| `get_top_queries(sort_by="resources", limit=10, interval_seconds=None)` | `list[dict]` — one per query |
| `analyze_workload_indexes(...)` / `analyze_query_indexes(queries, ...)` | `dict` with `summary`, `recommendations`, `query_impact` |

Each `HealthCheckResult` carries `healthy`, a text `summary`, the flagged
`findings` (one dict per object) and the `metrics` behind the verdict, and has
`to_dict()` for serialization.

Each `PlanFinding` carries its `issue`, `severity`, the `node_type` and
`relation` it is about, a `summary` and `detail`, `cost_ms` and
`share_of_time` for the work behind it, the `evidence`, and `remediation`
where there is a statement or a setting to change. It has `to_dict()` for
serialization.

Each `ModelFinding` carries its `rule`, `confidence` (`proven` or `candidate`),
`severity`, the `schema`/`table`/`columns` it is about, a `summary` and
`detail`, the `evidence` behind it, `remediation` (SQL statements, ready to
run), an optional `verify` query for candidates, and `impact_bytes` where the
cost is a size. It has `to_dict()` for serialization.

Failures raise instead of returning error strings. All exceptions derive from
`PgDebugError`: `ConnectionFailedError`, `NotConnectedError`,
`InvalidArgumentError`, `ExtensionRequiredError` (carries the missing
`extension` name), `ExplainPlanError`, `IndexTuningError`, `QueryError`,
`MissingDependencyError`.
A statement PostgreSQL rejects, or restricted mode refuses, raises
`QueryError`; when PostgreSQL rejected it, psycopg's exception, SQLSTATE and
all, is its `__cause__`.
A query context the server refuses, a role the connecting user is not a
member of or a setting it does not know, raises `InvalidArgumentError` when the
client connects, before any query depends on it.

### Without async

`SyncPgDebug` mirrors the same API, blocking instead of awaiting. It runs the
client on its own event loop, so close it (or use it as a context manager) when
you are done:

```python
from pg_debug import SyncPgDebug

with SyncPgDebug("postgresql://localhost/mydb") as pg:
    for row in pg.execute_sql("SELECT count(*) FROM orders"):
        print(row)
```

### Using the tools directly

If you already manage your own connection, the underlying tools take a `SqlDriver` and
can be used without `PgDebug`:

```python
from pg_debug import DatabaseHealthTool, SchemaInspector, SqlDriver, TopQueriesCalc
from pg_debug.plans import ExplainPlanTool
from pg_debug.index import DatabaseTuningAdvisor, TextPresentation

driver = SqlDriver(engine_url="postgresql://localhost/mydb")
checks = await DatabaseHealthTool(driver).run_checks("buffer")
```

The driver borrows a pooled connection per statement, so session state —
hypothetical indexes, `SET`, temp tables, advisory locks — does not survive
from one `execute_query` to the next. `session()` pins one connection for as
long as you need it:

```python
async with driver.session():
    await driver.execute_query("SELECT hypopg_create_index('CREATE INDEX ON orders (user_id)')")
    sizes = await driver.execute_query("SELECT indexname, hypopg_relation_size(indexrelid) FROM hypopg()")
```

A pinned driver holds a single connection, so do not share it across
concurrent tasks. Index tuning already wraps each analysis in one.

## The LLM tuning method

Both tune commands take `--method`. `dta` (the default) is a deterministic
search: it enumerates index combinations from the query's predicates and scores
each one against the planner. `llm` instead asks a language model for candidate
index sets, then scores those the same way — every suggestion is still
validated with HypoPG against the real planner, so the model proposes, it does
not decide.

It is worth trying when `dta` finds nothing and you suspect a non-obvious index
(an unusual column order, or a column the predicate only touches indirectly).
For everyday tuning, `dta` is faster, free and reproducible.

```bash
pip install 'pg-debug[llm]'
export OPENAI_API_KEY=sk-...

pg-debug tune-queries --method llm "SELECT * FROM orders WHERE user_id = 42 AND status = 'paid'"
```

What to expect:

- **One query at a time.** The optimizer reasons about a single plan, so
  `tune-queries --method llm` takes exactly one query and rejects more.
  `tune-workload --method llm` only works if the recorded workload narrows to a
  single query — otherwise use `dta` for workloads.
- **Several API calls per run**, at your own cost. It keeps proposing until
  five consecutive rounds fail to improve the objective.
- **HypoPG is still required** — that is what scores each proposal.
- **Non-deterministic.** It samples at a high temperature by design, so two
  runs on the same query can differ. Treat the output as candidates to review,
  not a verdict.
- **The model is configurable** three ways, most specific first: the
  `--llm-model` flag (or `llm_model=` on the client and per call),
  `PG_DEBUG_LLM_MODEL`, then the `gpt-4o` default. The client reads
  `OPENAI_API_KEY` and honours `OPENAI_BASE_URL`, so an OpenAI-compatible
  endpoint works if it supports structured outputs.

The same choice is available from the library:

```python
pg = PgDebug(url, llm_model="o3-mini")                       # for every call
result = await pg.analyze_query_indexes([query], method="llm")
result = await pg.analyze_query_indexes([query], method="llm", llm_model="gpt-4o")  # just this one
```

### Relationship to pganalyze

The live-activity, vacuum-progress, planner-statistics and interval-sampling
features were prompted by reading the
[pganalyze collector](https://github.com/pganalyze/collector), which is a good
map of which catalog views matter. No code was copied — the queries here were
written against the PostgreSQL catalogs directly. pg_debug's SQL parsing also
runs on pganalyze's
[libpg_query](https://github.com/pganalyze/libpg_query), via `pglast`.

### Known limitations

- Index tuning resolves schema-qualified table names directly. Unqualified
  names resolve through the connection's `search_path`, which `--search-path` /
  the `search_path` option sets.
- Index sizes come from `hypopg_relation_size()`, the planner's own estimate
  for the candidate. Where HypoPG produced no measurement, the advisor falls
  back to a formula over `pg_stats` and `pg_class.reltuples`, which is a rough
  figure — it assumes one index entry per live row and a fixed overhead factor.
- `analyze_query_indexes` (and `tune-queries`) accepts at most
  `MAX_NUM_INDEX_TUNING_QUERIES` (10) queries. `analyze_workload_indexes`
  (`tune-workload`) selects from `pg_stat_statements` instead: up to 100
  queries with at least 50 calls and a mean execution time of 5 ms or more.

### Environment variables

| Variable | Effect |
| --- | --- |
| `DATABASE_URI`, `DATABASE_URL` | Default connection URL. |
| `PG_DEBUG_INCLUDE_TRACE` | Set to `true` to include the index advisor's internal trace under `_trace` in tuning results. |
| `PG_DEBUG_LLM_MODEL` | Model for the `llm` tuning method (default `gpt-4o`). |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Credentials and endpoint for the `llm` tuning method. |

## Using it from Claude Code

pg-debug ships a Claude Code plugin whose skill teaches Claude how to drive
this tool: which command answers which question, how to read the output, and
the traps (extensions, `search_path`, access modes). Install it from this
repository, which is its marketplace:

```text
/plugin install pg-debug --marketplace danplischke/pg_debug
```

Claude Code before 2.1.275 needs the marketplace added first:

```text
/plugin marketplace add danplischke/pg_debug
/plugin install pg-debug@pg-debug
```

The skill then loads whenever you ask about slow queries, indexes, locks or
database health. It uses the `pg-debug` you have installed; without one, it
runs it with `uvx` from this repository rather than installing anything. The
plugin's version follows pg-debug's, so each release is a new version of the
skill: run `/plugin marketplace update pg-debug`, then update the plugin under
**Installed** in `/plugin`, or switch on auto-update for the marketplace there.

In a session started in this repository the same skill is a project skill —
`.claude/skills/pg-debug` links to it — so there is nothing to install.

## Development

```bash
uv sync --all-extras              # install into .venv
uv run pre-commit install         # ruff, pyright and uv.lock checks on every commit
uv run ruff format --check . && uv run ruff check . && uv run pyright
uv run pytest tests/unit          # unit tests, no database needed
uv run pytest                     # everything; spins up PostgreSQL containers, needs Docker
```

Some tests under `tests/unit` also start containers (`test_explain_real_db.py`,
`test_db_conn_pool.py`); see `tests/README.md`. [CONTRIBUTING.md](CONTRIBUTING.md)
covers conventions, the changelog and how a release is cut; [AGENTS.md](AGENTS.md)
is the same for coding agents.

### Package layout

| Under `src/pg_debug/` | What it holds |
| --- | --- |
| `client.py`, `sync.py`, `errors.py`, `severity.py`, `context.py` | The public surface: `PgDebug`, `SyncPgDebug`, the exceptions, how findings are graded (`Severity`, `Confidence`), and the role and settings queries run with (`QueryContext`, `pg-debug.toml`). |
| `cli/` | The `pg-debug` command: `parser.py` defines its arguments and which handler and renderer each subcommand uses, `commands.py` holds the handlers, `render.py` formats the results, `streams.py` reads input and writes output as UTF-8, and `config_file.py` finds `pg-debug.toml`. |
| `db/` | Talking to PostgreSQL: the driver and pool, restricted mode and its allowlist, extension and version checks, identifiers. |
| `parsing/` | Reading SQL pg_debug is given to analyse: the pglast visitors and bind-parameter substitution. |
| `queries/` | SQL that more than one module runs. A feature's own queries live beside the code that reads their rows. |
| `plans/` | `explain` and `diagnose`: running EXPLAIN, the plan tree, the diagnosis rules, hypothetical indexes. |
| `health/` | `health`: `tool.py` runs the checks in `checks/`, one module per check. |
| `index/` | `tune-workload` and `tune-queries`: the search in `base.py`, the `dta` and `llm` methods, and the report. |
| `index_use/` | `index-use`: the query's predicates, the catalog it compares them with, and the verdict for each index. |
| `model/`, `migration/`, `partition/` | `model`, `check-migration` and `partitions`. |
| `activity.py`, `configuration.py`, `schema.py`, `statistics.py`, `top_queries.py` | Features that fit in one file. |
| `baseline.py` | `--baseline` and `--save-baseline`: the identity each finding is known by, and the file that records them. |

A feature is one module until it needs a second file. `tests/unit/` mirrors
`src/pg_debug/`, and since the test directories have no package files, every
test module needs a name of its own.

## Relationship to postgres-mcp

The database logic — safe SQL parsing, the explain tooling, health calculators and the
Database Tuning Advisor — comes from
[crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp) (MIT, Copyright (c)
2025 Crystal Corp.) and is redistributed here under the same license. Changes made
during extraction:

- The MCP server, its tool registrations and transports are gone; `PgDebug` and the
  `pg-debug` command take their place.
- Tools return structured data instead of MCP text content, and raise exceptions
  instead of returning `"Error: ..."` strings.
- `TopQueriesCalc` returns rows rather than a stringified list; health checks return
  `HealthCheckResult` objects through `run_checks()` (`health()` still renders the
  same text report).
- The LLM tuning method's dependencies moved to the optional `llm` extra.
- `POSTGRES_MCP_INCLUDE_LANGFUSE_TRACE` became `PG_DEBUG_INCLUDE_TRACE`, and now
  defaults to off.
- The index advisor handles schema-qualified tables, and table size estimation
  no longer falls back to a fixed default for every table. See
  [CHANGELOG.md](CHANGELOG.md) for the full list.

## License

MIT. See [LICENSE](LICENSE); [NOTICE](NOTICE) records the derivation from postgres-mcp.
