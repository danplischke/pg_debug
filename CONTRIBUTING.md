# Contributing

## Setup

```bash
git clone https://github.com/danplischke/pg_debug && cd pg_debug
uv sync --all-extras          # Python 3.12+, installs into .venv with the llm extra and dev tools
uv run pre-commit install     # runs the checks below on every commit
```

With [just](https://just.systems/) installed, `just dev` does both, and `just`
lists the recipes in `.justfile` that wrap the commands in this guide:
`just lint`, `just test`, `just test-lowest`, `just check` for everything CI
runs, and `just release X.Y.Z`. None of them is required; the uv commands are
the same checks.

## Checks

CI runs exactly these, the tests on Python 3.12, 3.13 and 3.14 and on macOS
and Windows. A pull request has to pass them.

```bash
uv run ruff format --check .  # formatting (uv run ruff format . to fix)
uv run ruff check .           # lint
uv run pyright                # types, standard mode
uv run pytest                 # tests
```

Most tests are unit tests with mocked drivers and run in a few seconds:

```bash
uv run pytest tests/unit
```

Everything under `tests/integration/`, plus `tests/unit/plans/test_explain_real_db.py`
and `tests/unit/health/test_health_tool.py`, starts PostgreSQL 12, 15 and 16
containers and skips when Docker is not available. `tests/README.md` describes
the layout and how to run the end-to-end tests against a server you already
have instead.

One more job runs the unit tests with every direct dependency at the lowest
version `pyproject.toml` allows (`uv sync --all-extras --resolution
lowest-direct` on Python 3.12). When it fails, the code has started using
something newer than the floor says: raise the floor in the same change.

## Conventions

**Where code goes.** The package layout section of the README is the map. A
feature is one module until it needs a second file; a health check is one
module under `health/checks/`. `tests/unit/` mirrors `src/pg_debug/`, and since
the test directories have no package files, every test module needs a name of
its own.

**The public surface is pinned.** `tests/unit/test_public_api.py` lists the
names `pg_debug` exports, the CLI commands, and checks that every import in the
README and the skill still works. Changing any of them is an API change: make
it on purpose, by editing those lists in the same commit.

**New SQL goes in `src/pg_debug/queries/`.** `tests/unit/test_sql_location.py`
lists the modules that still write their own; that list only shrinks.

**Argue from what the server records.** A finding is backed by a counter,
catalog row or plan node, says which, and says whether it is proven or
inferred. Nothing guesses at hardware. A tool that cannot tell says so rather
than inventing a number.

**Every statement pg_debug runs is marked.** `SqlDriver` prefixes
`/* pg_debug */` so `top-queries` and the index advisor can leave the tool's
own queries out of the workload. Do not add the marker by hand, and do not
route the SQL around the driver.

**Changelog.** Any user-visible change gets an entry under **Unreleased** in
`CHANGELOG.md`, in the existing style: what the user saw before, what they see
now, and why. Internal moves are recorded there too when they change an import
path.

**Commits.** The subject says what the change does for the user, in the
imperative (`Diagnose autovacuum, not just wraparound`); the body says why and
what was ruled out. One change per commit.

**Documentation.** A new command or option is added to the README's command
reference and to `.claude/skills/pg-debug/SKILL.md`, which is how Claude Code
learns to drive the tool.

## Pull requests

Branch from `main`, open a pull request, and fill in the template. `main` is
protected by the ruleset in `.github/rulesets/main.json`: pull requests only,
linear history, every CI job green, review threads resolved. Squash or rebase
when merging.

## Releasing

The version is written in one place, `__version__` in
`src/pg_debug/__init__.py`; `pyproject.toml` reads it from there.

1. In `CHANGELOG.md`, turn **Unreleased** into `## [X.Y.Z] - YYYY-MM-DD`, start
   a new empty **Unreleased** section, and update the compare links at the
   bottom.
2. Set `__version__` to `X.Y.Z` and run `uv lock` so the lockfile agrees.
3. Commit as `Release X.Y.Z`, get it onto `main`, and wait for CI to pass.
4. Tag that commit and push the tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   `just release X.Y.Z` does the same from a checkout of `main`, after checking
   that `__version__` and the changelog both say `X.Y.Z`, the working tree is
   clean, and `main` is at `origin/main`.

The publish workflow (`.github/workflows/publish.yml`) then refuses unless the
tag matches `__version__` and the tagged commit is on `main`; runs the whole of
CI again on the tagged commit; builds the sdist and wheel; checks their
metadata; uploads them to PyPI through trusted publishing from the `pypi`
environment, only once CI has passed; and creates a GitHub release whose notes
are that version's changelog section.

### One-time setup for publishing

Trusted publishing needs no API token, but both sides have to be told about
each other once:

- **PyPI:** at <https://pypi.org/manage/account/publishing/>, add a pending
  publisher for project `pg-debug` with owner `danplischke`, repository
  `pg_debug`, workflow `publish.yml`, environment `pypi`. After the first
  release it is managed from the project's publishing settings instead.
- **GitHub:** in the repository's *Settings → Environments*, create `pypi`.
  Adding yourself as a required reviewer there makes every release wait for a
  click before it uploads.
- **Rulesets:** in *Settings → Rules → Rulesets*, import
  `.github/rulesets/main.json` and `.github/rulesets/release-tags.json`.
  `main.json` requires one status check, `CI result`, which passes only when
  every CI job did, so it needs re-importing only when the rules themselves
  change.
- **Codecov:** add the repository at <https://app.codecov.io> and store its
  upload token as the Actions secret `CODECOV_TOKEN`. Until then the coverage
  upload is attempted without a token and may be refused; that never fails
  CI.
- **Vulnerability reports:** in *Settings → Code security*, enable *Private
  vulnerability reporting*. `SECURITY.md` and the security link on the
  new-issue page send reporters to that form, and without the setting there is
  no form there.
- **Code scanning:** the CodeQL workflow uploads its results to GitHub code
  scanning, which private repositories on a personal account do not have. It
  fails until the repository is public and needs nothing after that.
