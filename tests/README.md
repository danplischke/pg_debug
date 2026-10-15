# pg_debug tests

## Running tests

```bash
uv run pytest tests/unit          # no database required for most of these
uv run pytest                     # everything, including container-backed tests
```

A single file or test:

```bash
uv run pytest tests/unit/db/test_obfuscate_password.py
uv run pytest tests/unit/db/test_db_conn_pool.py::test_pool_connect_success
```

## What needs a database

Most tests under `tests/unit/` are pure unit tests with mocked drivers and need
nothing. Three groups do talk to PostgreSQL:

| Tests | Requirement | Skipped when |
| --- | --- | --- |
| `tests/integration/`, `tests/unit/plans/test_explain_real_db.py`, `tests/unit/health/test_health_tool.py`, `tests/unit/health/checks/test_connection_real_db.py` | Docker — a `postgres:12`, `postgres:15` and `postgres:16` container per test class | Docker is unavailable |
| `tests/integration/test_pooler_integration.py` | Docker — one `postgres:16` container with PgBouncer started inside it, in transaction mode, without prepared statements and with round robin, as `PGBOUNCER_INI` in `utils.py` configures it | Docker is unavailable |
| `tests/integration/test_client_integration.py` | `PG_DEBUG_TEST_DATABASE_URI` pointing at any server you already have | The variable is unset |

The end-to-end client/CLI tests are the ones that do not need Docker:

```bash
PG_DEBUG_TEST_DATABASE_URI=postgresql://postgres@localhost:5432/postgres \
    uv run pytest tests/integration/test_client_integration.py
```

They create and drop a `pgdebug_test` schema plus a `pgdebug_test_orders`
table, so point them at a scratch database. Tests that need `hypopg` or
`pg_stat_statements` assert the missing-extension behaviour instead of skipping
when the extension is absent.

## Test layout

- `tests/unit/db/` — the driver and the errors it raises, restricted mode's parsing and rejection, connection pool, password obfuscation, the server version check
- `tests/unit/plans/` — explain plan generation and formatting, and plan diagnosis
- `tests/unit/index/` — the Database Tuning Advisor's candidate search, and what stops a tuning run
- `tests/unit/index_use/` — why a query does or does not use an index
- `tests/unit/model/`, `migration/`, `partition/` — the data model review, the migration review and the partition advisor
- `tests/unit/parsing/` — reading the SQL pg_debug is given, such as bind-parameter substitution
- `tests/unit/health/` — health check selection, and the individual checks under `checks/`
- `tests/unit/cli/` — the CLI and its rendering
- `tests/unit/queries/` — SQL shared between modules, such as the tuple layout
- `tests/unit/test_top_queries.py`, `test_top_queries_sampling.py` — `pg_stat_statements` queries across PostgreSQL versions
- `tests/unit/test_client.py`, `test_schema.py` and the other top-level files — the library API and the one-file features
- `tests/integration/` — end-to-end runs against a real server
