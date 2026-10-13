# pg_debug for coding agents

pg_debug is an async Python library and `pg-debug` CLI that inspects, diagnoses
and tunes a live PostgreSQL database. This file is about working on the
repository. `.claude/skills/pg-debug/SKILL.md` is about using the tool.

## Commands

```bash
uv sync --all-extras                                   # once; installs dev tools and the llm extra
uv run ruff format . && uv run ruff check . && uv run pyright
uv run pytest tests/unit                               # seconds, no database
uv run pytest                                          # adds the container-backed tests; needs Docker
uv run pytest tests/unit/plans/test_rules.py -k estimate  # one file or test
```

Run the first three before claiming a change is done. CI runs the same checks,
the tests on Python 3.12, 3.13 and 3.14 and on macOS and Windows, and fails on
any formatting or type error. `.justfile` wraps the same commands (`just lint`,
`just test`, `just check`) for people with `just` installed; the uv commands
above need nothing else.

## Layout

| Path | Holds |
| --- | --- |
| `src/pg_debug/client.py`, `sync.py`, `errors.py` | The public surface: `PgDebug`, `SyncPgDebug`, the exceptions. |
| `src/pg_debug/cli/` | The command; `render.py` formats output. |
| `src/pg_debug/db/` | Driver and pool, restricted mode and its allowlist, extension and version checks. |
| `src/pg_debug/parsing/` | pglast visitors and bind-parameter substitution for SQL we are *given*. |
| `src/pg_debug/queries/` | SQL that more than one module runs. A module's own SQL stays beside the code that reads its rows. |
| `src/pg_debug/plans/`, `health/`, `index/`, `model/`, `migration/`, `partition/` | One package per feature that needed more than one file. |
| `src/pg_debug/activity.py`, `configuration.py`, `schema.py`, `statistics.py`, `top_queries.py` | Features that fit in one file. |
| `tests/unit/` | Mirrors `src/pg_debug/`. Test module names must be unique across the tree. |
| `tests/integration/` | Container-backed; see `tests/README.md`. |

## Rules that tests enforce

- `tests/unit/test_public_api.py` pins the exported names, the CLI commands,
  and every import the README and skill show. If you change one, change the
  list there in the same commit and treat it as an API change in the
  changelog.
- `tests/unit/test_public_api.py` also checks `__version__` against the
  installed distribution. The version lives only in `src/pg_debug/__init__.py`.

## Rules the code follows

- **Every finding is argued from something the server recorded**: a counter, a
  catalog row, a plan node. It names its source and says whether it is proven
  or inferred. Nothing guesses at RAM or disk type. When the tool cannot tell,
  it says so instead of inventing a number.
- **Built to say no.** Advisors report what they examined and refused, with
  the figure the refusal rests on, rather than staying silent.
- **Every statement the tool runs is marked** `/* pg_debug */` by `SqlDriver`,
  so `top-queries` and the advisors can exclude it from the workload. Do not
  add the marker yourself, and do not bypass the driver.
- **Restricted mode is a promise.** Anything that is not read-only is refused
  before it reaches the server, and runs in a read-only transaction with a
  statement timeout. A new feature that needs to write must refuse in
  `restricted` mode with the reason, the way `diagnose` does.
- **Version-gate catalog differences** (`pg_stat_statements` column names,
  `pg_stat_checkpointer` in 17, vacuum progress counters) through the helpers
  in `db/`, not by catching errors.
- Errors are exceptions from `errors.py`, never `"Error: ..."` strings.
  Results are plain data (dicts and dataclasses); the CLI renders them.

## Changing things

- A user-visible change gets a `CHANGELOG.md` entry under **Unreleased**, in
  the existing style: what the user saw before, what they see now, why.
- A new command or option is documented in the README command reference and
  in `.claude/skills/pg-debug/SKILL.md`.
- Commit subjects say what the change does, in the imperative; bodies say why.
- Do not bump the version, edit release headings in the changelog, or create
  tags unless asked to cut a release. `CONTRIBUTING.md` has the procedure.
- Do not weaken a test to get green: a failing container test on a PostgreSQL
  version is a real difference between versions until proven otherwise.
