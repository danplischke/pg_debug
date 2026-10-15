# pg_debug for coding agents

pg_debug is an async Python library and `pg-debug` CLI that inspects, diagnoses
and tunes a live PostgreSQL database. This file is about working on the
repository. The skill in `plugins/pg-debug/skills/pg-debug/` is about using
the tool.

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
| `src/pg_debug/client.py`, `sync.py`, `errors.py`, `severity.py`, `context.py` | The public surface: `PgDebug`, `SyncPgDebug`, the exceptions, how findings are graded (`Severity`, `Confidence`), and the role and settings the user's queries run with (`QueryContext`, read from `pg-debug.toml`). |
| `src/pg_debug/cli/` | The command. `parser.py` names each subcommand's handler (in `commands.py`) and renderer (in `render.py`) in one place; `streams.py` reads and writes UTF-8; `config_file.py` finds `pg-debug.toml`. |
| `src/pg_debug/db/` | Driver and pool, restricted mode and its allowlist, extension and version checks, and what the statistics views withhold from the connecting role (`privileges.py`). |
| `src/pg_debug/parsing/` | pglast visitors and bind-parameter substitution for SQL we are *given*. |
| `src/pg_debug/queries/` | SQL that more than one module runs. A module's own SQL stays beside the code that reads its rows. |
| `src/pg_debug/plans/`, `health/`, `index/`, `index_use/`, `model/`, `migration/`, `partition/`, `sharding/` | One package per feature that needed more than one file. |
| `src/pg_debug/activity.py`, `configuration.py`, `history.py`, `schema.py`, `statistics.py`, `top_queries.py` | Features that fit in one file. |
| `src/pg_debug/baseline.py` | `--baseline`: the identity each finding is known by, and the file that records them. |
| `plugins/pg-debug/`, `.claude-plugin/marketplace.json` | The Claude Code plugin and the marketplace that publishes it. `.claude/skills/pg-debug` links to the plugin's skill. |
| `tests/unit/` | Mirrors `src/pg_debug/`. Test module names must be unique across the tree. |
| `tests/integration/` | Container-backed; see `tests/README.md`. |

## Rules that tests enforce

- `tests/unit/test_public_api.py` pins the exported names, the CLI commands,
  and every import the README and skill show. If you change one, change the
  list there in the same commit and treat it as an API change in the
  changelog.
- `tests/unit/test_public_api.py` also checks `__version__` against the
  installed distribution. The version lives in `src/pg_debug/__init__.py`;
  the plugin manifest repeats it, and `tests/unit/test_plugin.py` fails until
  the two agree.

## Rules the code follows

- **Every finding is argued from something the server recorded**: a counter, a
  catalog row, a plan node. It carries that evidence, so a reader can check
  the argument. A kind of finding that can be either read or inferred carries
  a `Confidence` from `severity.py`: `proven` when read from the catalog or the
  plan, `candidate` when inferred from sampled statistics or from a query's
  text, with a way to confirm it where there is one. The data model review and
  the index-use analysis do; a new kind of finding that infers does too.
  Nothing guesses at RAM or disk type. When the tool cannot tell, it says so
  instead of inventing a number.
- **Built to say no.** Advisors report what they examined and refused, with
  the figure the refusal rests on, rather than staying silent.
- **Every statement the tool runs is marked** `/* pg_debug */` by `SqlDriver`,
  so `top-queries` and the advisors can exclude it from the workload. Do not
  add the marker yourself, and do not bypass the driver.
- **The query context is for the user's query.** A statement that runs or
  plans SQL the user handed over, or reads a setting that shaped it, passes
  `in_context=True`, so it runs with the role and settings the user declared.
  pg-debug's own catalog reads do not: the context's role may not be allowed
  them, and row-level security hides statistics from it. A new feature that
  runs the user's SQL marks it the same way.
- **A transaction is the only session there is.** Behind a transaction
  pooler (PgBouncer, Supavisor, Neon's and Supabase's pooled endpoints) each
  transaction may reach a different backend, and whatever one leaves in its
  backend stays for the pooler's next client. So nothing pg-debug needs may
  outlive the statement that needs it: a setting goes in the query context
  (`driver.with_context(...)`), never a `SET`; statements that share backend
  state go in one `execute_query` call, and state a rollback does not undo,
  such as hypothetical indexes, is undone by its `cleanup`. `session()` saves
  borrowing connections; it does not make a session. Nothing is prepared on
  the server. `tests/integration/test_pooler_integration.py` runs the commands
  through PgBouncer with round robin, and a new command that keeps state
  belongs there too.
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
  in the skill: a row in the table in
  `plugins/pg-debug/skills/pg-debug/SKILL.md`, and the detail in the reference
  file for its area.
- Commit subjects say what the change does, in the imperative; bodies say why.
- Do not bump the version, edit release headings in the changelog, or create
  tags unless asked to cut a release. `CONTRIBUTING.md` has the procedure.
- Do not weaken a test to get green: a failing container test on a PostgreSQL
  version is a real difference between versions until proven otherwise.
