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
| `tests/integration/`, `tests/unit/plans/test_explain_real_db.py`, `tests/unit/database_health/test_database_health_tool.py` | Docker — a `postgres:12`, `postgres:15` and `postgres:16` container per test class | Docker is unavailable |
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

- `tests/unit/db/` — the driver, restricted mode's parsing and rejection, connection pool, password obfuscation
- `tests/unit/plans/` — explain plan generation and formatting, and plan diagnosis
- `tests/unit/index/` — the Database Tuning Advisor's candidate search
- `tests/unit/database_health/` — health check selection and the individual calculators
- `tests/unit/top_queries/` — `pg_stat_statements` queries across PostgreSQL versions
- `tests/unit/test_client.py`, `test_cli.py`, `test_schema.py`, `test_formatting.py` — the library API, the CLI and its rendering
- `tests/integration/` — end-to-end runs against a real server
