set shell := ['sh', '-cu']

@_:
    just --list

# Install the package with the llm extra and the dev tools, and the pre-commit hooks
[group('development')]
dev:
    uv sync --all-extras
    uv run pre-commit install

# Format, and apply the lint fixes ruff can make
[group('development')]
format:
    uv run ruff format .
    uv run ruff check . --fix

# Format check, lint and type check, as the lint job does
[group('development')]
lint:
    uv run ruff format --check .
    uv run ruff check .
    uv run pyright

# Run the pre-commit hooks on all files
[group('development')]
pc:
    uv run pre-commit run --all-files

# Unit tests, no database needed, e.g. `just test -k estimate`
[group('test')]
test *args:
    uv run pytest tests/unit {{ args }}

# Every test; the container-backed ones start PostgreSQL 12, 15 and 16 and skip without Docker
[group('test')]
test-all *args:
    uv run pytest {{ args }}

# Unit tests with every direct dependency at its lowest allowed version, as the lowest job does; in .cache/lowest so uv.lock and .venv are left alone
[group('test')]
test-lowest *args:
    #!/usr/bin/env sh
    set -eu
    copy="$PWD/.cache/lowest"
    mkdir -p "$copy"
    rsync -a --delete --exclude .git --exclude .venv --exclude .cache ./ "$copy/"
    cd "$copy"
    uv sync --quiet --all-extras --resolution lowest-direct --python 3.12
    uv pip list | grep -iE '^(attrs|humanize|instructor|pglast|psycopg|psycopg-pool|pydantic) '
    uv run --no-sync pytest tests/unit -q {{ args }}

# Lint, every test, package build and smoke test, as the CI does
[group('test')]
check: lint test-all smoke

# Build the sdist and wheel into dist/ and check their metadata
[group('package')]
build:
    rm -rf dist
    uv build
    uvx twine check --strict dist/*

# Install the wheel into a clean environment and run it, as the package job does
[group('package')]
smoke: build
    #!/usr/bin/env sh
    set -eu
    venv="$PWD/.cache/smoke"
    rm -rf "$venv"
    uv venv --quiet "$venv"
    uv pip install --quiet --python "$venv/bin/python" dist/*.whl
    "$venv/bin/pg-debug" --version
    "$venv/bin/python" -m pg_debug --version

[group('package')]
lock:
    uv lock

# Upgrade the locked dependencies to their latest allowed versions
[group('package')]
upgrade:
    uv lock --upgrade

[group('package')]
versions:
    @git tag -l "v*" --sort=-v:refname | head -10

# Tag v<ver> on a clean main that matches origin and push it; the publish workflow does the rest
[group('publish')]
release ver:
    #!/usr/bin/env sh
    set -eu
    version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/pg_debug/__init__.py)
    [ "$version" = "{{ ver }}" ] || { echo "__version__ is $version, not {{ ver }}; see Releasing in CONTRIBUTING.md"; exit 1; }
    grep -q "^## \[{{ ver }}\] - " CHANGELOG.md || { echo "CHANGELOG.md has no '## [{{ ver }}] - <date>' section"; exit 1; }
    [ "$(git branch --show-current)" = "main" ] || { echo "release from main"; exit 1; }
    [ -z "$(git status --porcelain)" ] || { echo "the working tree is not clean"; exit 1; }
    git fetch --quiet origin main
    [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "main is not at origin/main"; exit 1; }
    git tag -a "v{{ ver }}" -m "Release {{ ver }}"
    git push origin "v{{ ver }}"
    echo "Pushed v{{ ver }}; the publish workflow runs CI, then uploads to PyPI."

[group('maintenance')]
clean:
    rm -rf dist .cache .pytest_cache .ruff_cache .coverage coverage.xml
