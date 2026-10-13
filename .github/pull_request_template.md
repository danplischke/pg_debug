## What changes, and why

<!-- The user-visible behaviour before and after. Link the issue if there is one. -->

## How it was verified

<!-- Which tests cover it. If it touches SQL, which PostgreSQL versions it ran against. -->

## Checklist

- [ ] `uv run ruff format --check . && uv run ruff check . && uv run pyright` pass
- [ ] Tests added or updated under `tests/unit/`, mirroring the source module
- [ ] A `CHANGELOG.md` entry under **Unreleased** for any user-visible change
- [ ] README and the skill in `plugins/pg-debug/skills/pg-debug/` updated if a command or option changed
