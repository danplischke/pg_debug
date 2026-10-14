# From Python

The `pg_debug` library under the CLI: the client, its errors, its results, and sessions.
Using it means installing it into the project's environment, so ask first:
`uv add git+https://github.com/danplischke/pg_debug`, or the same source with
`pip install`.

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
            if check.error:
                print(name, "could not run:", check.error)
            elif not check.healthy:
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

On a scale-to-zero platform that matters more. Once connected, an open client
keeps one pooled connection open, and an Aurora serverless instance does not
auto-pause while any connection is open. Open a client for a piece of work and
close it after (`async with`, `with` for `SyncPgDebug`, or `close()`) rather
than holding one for the life of a long-running process; a pool handed in with
`pool=` is not closed with the client, so close it too. Connecting wakes a
suspended Neon compute and resets its idle timer, and any connection attempt,
even one that fails to log in, resumes a paused Aurora serverless instance,
which takes about 15 seconds. The first timings after a wake run cold.

Failures raise rather than returning error strings, except in
`analyze_db_health()`, which returns a check that could not run with its
`error` and runs the rest. Catch `PgDebugError`, or
the specific subclass — `ConnectionFailedError`, `InvalidArgumentError`,
`ExtensionRequiredError` (has `.extension`), `ExplainPlanError`, `QueryError`
(a statement PostgreSQL rejected or restricted mode refused),
`IndexTuningError` (tables never analyzed, or nothing in the workload could
be analyzed), `MissingDependencyError`.

Returns: `list[dict]` for schemas/objects/rows/top-queries/activity/vacuum
progress/column statistics, `dict` for `get_object_details` and the tuning
results, `ExplainPlanArtifact` for plans, `dict[str, HealthCheckResult]` for
health, and `list[ModelFinding]` for the data model review.

The live-activity and statistics methods mirror the commands:
`list_activity(include_idle=, min_duration_seconds=, blocked_only=)`,
`lock_tree()`, `vacuum_progress()`, `explain_index_use(sql, index=)`, `column_stats(table)`, `extended_statistics(table)`, and
`get_top_queries(..., interval_seconds=)` for sampling. The model review is
`review_data_model(rules=, schema=, min_rows=, proven_only=)`; each
`ModelFinding` has `rule`, `confidence`, `severity`, `summary`, `detail`,
`evidence`, `remediation`, `verify` and `impact_bytes`, plus `to_dict()`.

To run the user's queries as their application does, with its role and the
session variables its row-level security policies read, give the client a
`QueryContext` rather than issuing `SET` yourself. It applies to
`execute_sql`, `explain_query`, `diagnose_query`, `explain_index_use` and index
tuning, inside each statement's own transaction; the client's catalog reads
stay with the connecting user. A context the server refuses raises
`InvalidArgumentError` when the client connects.

```python
from pg_debug import PgDebug, QueryContext

context = QueryContext(role="app_user", settings={"app.tenant_id": "7", "work_mem": "64MB"})
async with PgDebug("postgresql://localhost/mydb", context=context) as pg:
    plan = await pg.explain_query("SELECT * FROM orders WHERE total > 100")

context = QueryContext.from_file("pg-debug.toml")   # the [context] the CLI reads
```

Each `execute_sql` call may land on a different pooled connection, and behind
a transaction pooler (PgBouncer in transaction mode, Supavisor, a `-pooler`
host) on a different server backend, so session state does not carry over: a
`SET`, a temp table, an advisory lock or a hypothetical index created in one
call is not guaranteed to exist in the next, and a `BEGIN` will not hold. What
has to share a backend has to share a transaction: put the statements in one
`execute_query` call on the driver, and undo backend state with a `cleanup`,
which runs in the same transaction even when the statements fail:

```python
driver = await pg.sql_driver()
rows = await driver.execute_query(
    "SELECT hypopg_create_index('CREATE INDEX ON orders (user_id)');"
    "EXPLAIN SELECT * FROM orders WHERE user_id = 42",
    cleanup="SELECT hypopg_reset()",
)
```

A setting is not a `SET` either, nor a `SET LOCAL`, which ends with the
statement that made it: a `QueryContext` is the way to run a query with a
setting. `driver.with_context(context)` gives a driver that applies one to
the statements it runs with `in_context=True`, and to no others:

```python
tuned = driver.with_context(QueryContext(settings={"work_mem": "256MB"}))
rows = await tuned.execute_query("EXPLAIN ANALYZE SELECT ...", in_context=True)
```

`driver.session()` pins one pooled connection for a block of statements; that
is one session on a direct connection, but not behind a transaction pooler.

`SqlDriver(conn=...)` takes a `DbConnPool`, which already prepares nothing on
the server, or a single psycopg connection. If you open that connection
yourself and it may go through a transaction pooler, open it with
`psycopg.AsyncConnection.connect(url, prepare_threshold=None)` (or build the
psycopg pool you borrow it from with `kwargs={"prepare_threshold": None}`):
psycopg prepares a statement it has run five times, and a pooler without
prepared-statement support fails it with
`prepared statement "_pg3_0" already exists`.

On a `SQL_ASCII` database also pass `client_encoding="UTF8"`, or the encoding
the data is really in: `psycopg.AsyncConnection.connect(url,
prepare_threshold=None, client_encoding="UTF8")`. Left to libpq, such a
connection gets SQL_ASCII, psycopg returns every text column as `bytes`, and
`SqlDriver` refuses it with a `QueryError` that says so. A `DbConnPool` makes
this choice itself.
