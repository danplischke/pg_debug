"""QueryContext: the role and settings the user's queries run with, and pg-debug.toml."""

import pytest

from pg_debug import QueryContext
from pg_debug.errors import InvalidArgumentError


def write(tmp_path, text: str):
    path = tmp_path / "pg-debug.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- The context ------------------------------------------------------------


def test_an_empty_context_has_nothing_to_apply():
    assert not QueryContext()
    assert QueryContext(role="app_user")
    assert QueryContext(settings={"work_mem": "64MB"})


@pytest.mark.parametrize(
    ("name", "owner"),
    [
        ("statement_timeout", "--timeout"),
        ("search_path", "--search-path"),
        ("role", "a role instead"),
        ("session_authorization", "a role instead"),
        ("transaction_read_only", "--access-mode"),
        ("default_transaction_read_only", "--access-mode"),
    ],
)
def test_a_setting_pg_debug_owns_is_refused_with_its_owner(name, owner):
    with pytest.raises(InvalidArgumentError, match=f"{name} cannot be set in the query context.*{owner}"):
        QueryContext(settings={name: "0"})


def test_setting_names_are_compared_without_case_as_postgresql_does():
    with pytest.raises(InvalidArgumentError, match="Statement_Timeout cannot be set"):
        QueryContext(settings={"Statement_Timeout": "0"})
    with pytest.raises(InvalidArgumentError, match="work_mem and WORK_MEM are the same setting"):
        QueryContext(settings={"work_mem": "1MB", "WORK_MEM": "2MB"})


def test_values_are_text_as_set_config_takes_them():
    with pytest.raises(InvalidArgumentError, match="value of work_mem must be text"):
        QueryContext(settings={"work_mem": 64})  # type: ignore[dict-item]


def test_an_empty_role_is_refused_rather_than_read_as_none():
    with pytest.raises(InvalidArgumentError, match="role must be a role name"):
        QueryContext(role="")


def test_the_settings_cannot_change_after_they_were_checked():
    context = QueryContext(settings={"work_mem": "64MB"})

    with pytest.raises(TypeError):
        context.settings["statement_timeout"] = "0"  # type: ignore[index]


def test_a_context_is_hashable_and_compares_by_value():
    assert QueryContext(role="a", settings={"x.y": "1"}) == QueryContext(role="a", settings={"x.y": "1"})
    assert len({QueryContext(role="a"), QueryContext(role="a")}) == 1


def test_describe_names_the_role_then_the_settings_in_order():
    context = QueryContext(role="app_user", settings={"app.tenant_id": "t7", "work_mem": "64MB"})

    assert context.describe() == "role app_user, app.tenant_id=t7, work_mem=64MB"


def test_describe_quotes_a_value_that_would_read_ambiguously():
    context = QueryContext(settings={"app.name": "Acme, Inc", "app.empty": "", "app.quote": "it's"})

    assert context.describe() == "app.name='Acme, Inc', app.empty='', app.quote='it''s'"


def test_overrides_replace_the_role_and_same_named_settings():
    context = QueryContext(role="app_user", settings={"work_mem": "64MB", "app.tenant_id": "t7"})

    overridden = context.with_overrides(role="admin", settings={"WORK_MEM": "8MB"})

    assert overridden.role == "admin"
    assert dict(overridden.settings) == {"app.tenant_id": "t7", "WORK_MEM": "8MB"}


def test_no_overrides_keep_the_context():
    context = QueryContext(role="app_user", settings={"work_mem": "64MB"})

    assert context.with_overrides() == context


# -- pg-debug.toml ----------------------------------------------------------


def test_a_file_declares_a_role_and_settings(tmp_path):
    path = write(
        tmp_path,
        """
        [context]
        role = "app_user"

        [context.settings]
        "app.tenant_id" = "t7"
        work_mem = "64MB"
        """,
    )

    assert QueryContext.from_file(path) == QueryContext(role="app_user", settings={"app.tenant_id": "t7", "work_mem": "64MB"})


def test_a_dotted_name_may_be_written_quoted_or_as_a_dotted_key(tmp_path):
    """Unquoted, TOML reads app.tenant_id as a table app holding tenant_id."""
    path = write(
        tmp_path,
        """
        [context.settings]
        app.tenant_id = "t7"
        request.jwt.claims = '{"sub": "42"}'

        [context.settings.myapp]
        region = "eu"
        """,
    )

    settings = dict(QueryContext.from_file(path).settings)

    assert settings == {"app.tenant_id": "t7", "request.jwt.claims": '{"sub": "42"}', "myapp.region": "eu"}


def test_a_name_written_both_ways_is_set_twice(tmp_path):
    path = write(
        tmp_path,
        """
        [context.settings]
        "app.tenant_id" = "t7"
        app.tenant_id = "t8"
        """,
    )

    with pytest.raises(InvalidArgumentError, match=r"app\.tenant_id is set twice"):
        QueryContext.from_file(path)


def test_numbers_and_booleans_become_the_text_set_config_takes(tmp_path):
    path = write(
        tmp_path,
        """
        [context.settings]
        work_mem = 65536
        random_page_cost = 1.1
        enable_seqscan = false
        "app.flag" = true
        """,
    )

    assert dict(QueryContext.from_file(path).settings) == {
        "work_mem": "65536",
        "random_page_cost": "1.1",
        "enable_seqscan": "false",
        "app.flag": "true",
    }


@pytest.mark.parametrize(("value", "kind"), [("[1, 2]", "list"), ("1979-05-27", "date")])
def test_a_value_set_config_cannot_take_is_refused(tmp_path, value, kind):
    path = write(tmp_path, f"[context.settings]\nwork_mem = {value}\n")

    with pytest.raises(InvalidArgumentError, match=f"work_mem must be a string, a number or a boolean, not {kind}"):
        QueryContext.from_file(path)


def test_a_file_without_a_context_declares_none(tmp_path):
    assert not QueryContext.from_file(write(tmp_path, ""))


def test_an_unknown_key_is_an_error_rather_than_ignored(tmp_path):
    with pytest.raises(InvalidArgumentError, match="Unknown key database_url"):
        QueryContext.from_file(write(tmp_path, 'database_url = "postgresql://localhost/db"\n'))


def test_an_unknown_key_in_the_context_is_an_error(tmp_path):
    with pytest.raises(InvalidArgumentError, match=r"Unknown key rol in \[context\]. It takes role and settings"):
        QueryContext.from_file(write(tmp_path, '[context]\nrol = "app_user"\n'))


def test_a_role_that_is_not_a_string_is_an_error(tmp_path):
    with pytest.raises(InvalidArgumentError, match="role must be a string, not int"):
        QueryContext.from_file(write(tmp_path, "[context]\nrole = 7\n"))


def test_settings_that_are_not_a_table_are_an_error(tmp_path):
    with pytest.raises(InvalidArgumentError, match="settings must be a table"):
        QueryContext.from_file(write(tmp_path, '[context]\nsettings = "work_mem=64MB"\n'))


def test_an_error_in_the_context_names_the_file(tmp_path):
    path = write(tmp_path, "[context.settings]\nstatement_timeout = 0\n")

    with pytest.raises(InvalidArgumentError) as excinfo:
        QueryContext.from_file(path)

    assert str(excinfo.value).startswith(f"{path}: statement_timeout cannot be set in the query context")


def test_a_file_that_is_not_toml_names_the_file_and_the_line(tmp_path):
    path = write(tmp_path, "[context]\nrole = app_user\n")

    with pytest.raises(InvalidArgumentError, match=r"pg-debug.toml is not valid TOML: .*line 2"):
        QueryContext.from_file(path)


def test_a_file_a_windows_editor_saved_with_a_byte_order_mark_is_read(tmp_path):
    path = tmp_path / "pg-debug.toml"
    path.write_bytes(b"\xef\xbb\xbf" + b'[context]\nrole = "app_user"\n')

    assert QueryContext.from_file(path) == QueryContext(role="app_user")


def test_a_file_that_is_not_utf8_says_so(tmp_path):
    path = tmp_path / "pg-debug.toml"
    path.write_bytes('[context]\nrole = "caf\u00e9"\n'.encode("latin-1"))

    with pytest.raises(InvalidArgumentError, match="is not UTF-8"):
        QueryContext.from_file(path)


def test_a_file_that_cannot_be_read_says_so(tmp_path):
    with pytest.raises(InvalidArgumentError, match=r"Could not read .*missing\.toml"):
        QueryContext.from_file(tmp_path / "missing.toml")
