import pytest


@pytest.fixture(autouse=True)
def away_from_any_config_file(monkeypatch, tmp_path):
    """Run each command from an empty directory.

    A command that takes a query context reads pg-debug.toml from the working
    directory and its parents, so a file in a developer's home directory
    would otherwise reach every test that runs one.
    """
    monkeypatch.chdir(tmp_path)
