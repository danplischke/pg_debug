"""Fingerprints are of what a finding is about, read in one restricted-safe statement."""

import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.definitions import DEFINITIONS_SQL
from pg_debug.definitions import definition_fingerprints
from pg_debug.definitions import fingerprint
from pg_debug.definitions import targets


def row(**cells):
    result = MagicMock()
    result.cells = cells
    return result


def test_an_index_finding_is_fingerprinted_by_its_index_and_a_duplicate_with_its_cover():
    assert targets("health", {"check": "Index bloat", "schema": "s", "table": "t", "index": "i"}) == [{"kind": "index", "schema": "s", "name": "i"}]
    assert targets("health", {"check": "Duplicate index check", "schema": "s", "table": "t", "index": "i", "covered_by": "j"}) == [
        {"kind": "index", "schema": "s", "name": "i"},
        {"kind": "index", "schema": "s", "name": "j"},
    ]


def test_a_constraint_finding_is_fingerprinted_by_its_constraint():
    key = {"check": "Constraint health", "schema": "s", "table": "t", "name": "t_fk"}
    assert targets("health", key) == [{"kind": "constraint", "schema": "s", "table": "t", "name": "t_fk"}]


def test_a_finding_about_a_tables_state_or_a_setting_has_no_fingerprint():
    assert targets("health", {"check": "Table bloat", "schema": "s", "table": "t"}) == []
    assert targets("health", {"check": "Connection health", "kind": "idle_in_transaction", "usename": "app"}) == []
    assert targets("config", {"setting": "work_mem"}) == []


def test_a_model_finding_is_fingerprinted_by_its_columns_or_its_table():
    assert targets("model", {"rule": "naive_timestamp", "schema": "s", "table": "t", "columns": ["at"]}) == [
        {"kind": "columns", "schema": "s", "table": "t", "columns": ["at"]}
    ]
    assert targets("model", {"rule": "missing_primary_key", "schema": "s", "table": "t", "columns": []}) == [
        {"kind": "table", "schema": "s", "table": "t"}
    ]


def test_restricted_mode_runs_the_definitions_query():
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(SafeSqlDriver.param_sql_to_query(DEFINITIONS_SQL, [json.dumps([{"i": 0}])]))


@pytest.mark.asyncio
async def test_one_statement_reads_every_definition_and_a_missing_one_gives_none():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(
        return_value=[
            row(i=0, definition="CREATE INDEX i ON s.t (a)"),
            row(i=1, definition="CREATE INDEX j ON s.t (a, b)"),
            row(i=2, definition=None),
        ]
    )
    keys = [
        {"check": "Duplicate index check", "schema": "s", "table": "t", "index": "i", "covered_by": "j"},
        {"check": "Table bloat", "schema": "s", "table": "t"},
        {"check": "Index bloat", "schema": "s", "table": "t", "index": "gone"},
    ]

    result = await definition_fingerprints(driver, "health", keys)

    assert result == [fingerprint("CREATE INDEX i ON s.t (a)\nCREATE INDEX j ON s.t (a, b)"), None, None]
    driver.execute_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_nothing_to_fingerprint_reads_nothing():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock()

    assert await definition_fingerprints(driver, "config", [{"setting": "work_mem"}]) == [None]
    driver.execute_query.assert_not_awaited()
