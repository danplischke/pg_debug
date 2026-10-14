"""How a command finds its query context: pg-debug.toml, --config, --no-config, --role and --set."""

import argparse
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_debug import QueryContext
from pg_debug import cli
from pg_debug.cli.config_file import find_config_file
from pg_debug.cli.config_file import resolve_context
from pg_debug.cli.parser import parse_setting

FILE = """
[context]
role = "app_user"

[context.settings]
app.tenant_id = "t7"
work_mem = "64MB"
"""


def parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


def run(argv):
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with pg-debug.toml at its root, worked on from a subdirectory."""
    (tmp_path / "pg-debug.toml").write_text(FILE, encoding="utf-8")
    deeper = tmp_path / "src" / "app"
    deeper.mkdir(parents=True)
    monkeypatch.chdir(deeper)
    return tmp_path


# -- Finding the file -------------------------------------------------------


def test_the_nearest_file_up_the_tree_is_found(project):
    nested = project / "src" / "pg-debug.toml"
    nested.write_text("", encoding="utf-8")

    assert find_config_file(project / "src" / "app") == nested
    assert find_config_file(project) == project / "pg-debug.toml"


def test_no_file_anywhere_up_the_tree_is_none(tmp_path):
    assert find_config_file(tmp_path) is None


# -- Resolving the context --------------------------------------------------


def test_a_command_that_takes_no_context_gets_none(project):
    assert resolve_context(parse("health")) == (None, None)


def test_the_file_found_up_the_tree_is_the_context(project):
    context, source = resolve_context(parse("execute", "SELECT 1"))

    assert context == QueryContext(role="app_user", settings={"app.tenant_id": "t7", "work_mem": "64MB"})
    assert source == str(project / "pg-debug.toml")


def test_no_config_ignores_the_file(project):
    assert resolve_context(parse("execute", "--no-config", "SELECT 1")) == (None, None)


def test_config_reads_the_file_it_names(project, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "staging.toml"
    other.write_text('[context]\nrole = "reporting"\n', encoding="utf-8")

    context, source = resolve_context(parse("explain", "--config", str(other), "SELECT 1"))

    assert context == QueryContext(role="reporting")
    assert source == str(other)


def test_the_command_line_overrides_the_file(project):
    context, source = resolve_context(parse("diagnose", "--role", "admin", "--set", "WORK_MEM=8MB", "SELECT 1"))

    assert context == QueryContext(role="admin", settings={"app.tenant_id": "t7", "WORK_MEM": "8MB"})
    assert source == f"{project / 'pg-debug.toml'} and the command line"


def test_the_command_line_alone_is_a_context(tmp_path):
    context, source = resolve_context(parse("tune-queries", "--set", "app.tenant_id=t7", "SELECT 1"))

    assert context == QueryContext(settings={"app.tenant_id": "t7"})
    assert source == "the command line"


def test_nothing_anywhere_is_no_context(tmp_path):
    assert resolve_context(parse("index-use", "SELECT 1")) == (None, None)


# -- Parsing --set ----------------------------------------------------------


def test_a_value_may_hold_an_equals_sign():
    assert parse_setting('request.jwt.claims={"a":"b=c"}') == ("request.jwt.claims", '{"a":"b=c"}')


def test_an_empty_value_is_a_value():
    assert parse_setting("app.tenant_id=") == ("app.tenant_id", "")


@pytest.mark.parametrize("text", ["work_mem", "=64MB", " =64MB"])
def test_a_setting_needs_a_name_and_an_equals_sign(text):
    with pytest.raises(argparse.ArgumentTypeError, match="Write it as NAME=VALUE"):
        parse_setting(text)


# -- The command line -------------------------------------------------------


@pytest.fixture
def pg_client():
    with patch("pg_debug.cli.PgDebug") as client_cls:
        client = client_cls.return_value
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.execute_sql = AsyncMock(return_value=[])
        client.analyze_db_health = AsyncMock(return_value={})
        yield client_cls


def test_a_command_says_what_context_it_runs_in_and_passes_it_on(project, pg_client):
    code, _, err = run(["execute", "SELECT 1"])

    assert code == 0
    assert err == f"pg-debug: query context: role app_user, app.tenant_id=t7, work_mem=64MB (from {project / 'pg-debug.toml'})\n"
    assert pg_client.call_args.kwargs["context"] == QueryContext(role="app_user", settings={"app.tenant_id": "t7", "work_mem": "64MB"})


def test_a_command_that_takes_no_context_neither_reads_nor_mentions_the_file(project, pg_client):
    (project / "pg-debug.toml").write_text("not toml at all", encoding="utf-8")

    code, _, err = run(["health"])

    assert code == 0
    assert err == ""
    assert pg_client.call_args.kwargs["context"] is None


def test_a_broken_file_is_an_error_that_names_it(project, pg_client):
    (project / "pg-debug.toml").write_text("[context.settings]\nstatement_timeout = 0\n", encoding="utf-8")

    code, _, err = run(["execute", "SELECT 1"])

    assert code == 1
    assert err.startswith(f"pg-debug: {project / 'pg-debug.toml'}: statement_timeout cannot be set in the query context")
    pg_client.assert_not_called()


def test_the_context_options_belong_to_the_commands_that_use_them(pg_client):
    with pytest.raises(SystemExit) as excinfo:
        run(["health", "--role", "app_user"])

    assert excinfo.value.code == 2


def test_config_and_no_config_are_one_or_the_other(pg_client):
    with pytest.raises(SystemExit) as excinfo:
        run(["execute", "--config", "a.toml", "--no-config", "SELECT 1"])

    assert excinfo.value.code == 2
