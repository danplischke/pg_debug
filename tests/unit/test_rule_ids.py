"""Every kind of finding has a stable public id, a documented entry, and says what it reads."""

import re
from pathlib import Path

import pytest

from pg_debug.errors import InvalidArgumentError
from pg_debug.health.checks import autovacuum
from pg_debug.health.checks import buffer
from pg_debug.health.checks import connection
from pg_debug.health.checks import constraint
from pg_debug.health.checks import index
from pg_debug.health.checks import replication
from pg_debug.health.checks import seq_scan
from pg_debug.health.checks import sequence
from pg_debug.health.checks import statistics
from pg_debug.health.checks import table_bloat
from pg_debug.health.checks import vacuum
from pg_debug.model import RuleId
from pg_debug.rules import DOCS_URL
from pg_debug.rules import RULES
from pg_debug.rules import Needs
from pg_debug.rules import check_rules
from pg_debug.rules import health_rule
from pg_debug.rules import is_static_check
from pg_debug.rules import parse_rule_ids
from pg_debug.rules import rules_of

REPOSITORY = Path(__file__).resolve().parents[2]
REFERENCE = REPOSITORY / "plugins" / "pg-debug" / "skills" / "pg-debug" / "references" / "rules.md"
CONFIGURATION = REPOSITORY / "src" / "pg_debug" / "configuration.py"

CHECK_NAMES = {
    index.INVALID_INDEXES,
    index.DUPLICATE_INDEXES,
    index.INDEX_BLOAT,
    index.UNUSED_INDEXES,
    connection.CONNECTION_HEALTH,
    vacuum.VACUUM_HEALTH,
    autovacuum.AUTOVACUUM_BACKLOG,
    autovacuum.VACUUM_HORIZON,
    sequence.SEQUENCE_HEALTH,
    replication.REPLICATION_HEALTH,
    buffer.INDEX_HIT_RATE,
    buffer.TABLE_HIT_RATE,
    constraint.CONSTRAINT_HEALTH,
    statistics.STATISTICS_HEALTH,
    table_bloat.TABLE_BLOAT,
    seq_scan.SEQ_SCAN_HEALTH,
}


def test_every_health_check_has_its_rules_and_no_rule_names_a_check_that_is_not_there():
    assert {r.check for r in rules_of("health")} == CHECK_NAMES
    assert all(check_rules(name) for name in CHECK_NAMES)


def test_the_connection_checks_kinds_are_its_rule_ids():
    assert {r.id for r in check_rules(connection.CONNECTION_HEALTH)} == {connection.CONNECTIONS_IN_USE, connection.IDLE_IN_TRANSACTION}
    assert health_rule(connection.CONNECTION_HEALTH, {"kind": "idle_in_transaction"}).id == "idle_in_transaction"  # type: ignore[union-attr]


def test_every_model_rule_is_registered_under_its_own_value():
    assert {r.id for r in rules_of("model")} == {r.value for r in RuleId if r is not RuleId.ALL}


def test_every_rule_the_configuration_review_reports_is_registered():
    """The ids are written where each finding is made; one missing from the
    registry would be a finding nobody can suppress or look up."""
    written = set(re.findall(r'rule="(\w+)"', CONFIGURATION.read_text())) | {"fsync_off", "full_page_writes_off"}
    assert written == {r.id for r in rules_of("config")}


def test_ids_are_lower_case_words_and_unique_across_commands():
    ids = [r.id for r in RULES.values()]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z][a-z_]*", rule_id) for rule_id in ids)


def test_every_rule_has_an_entry_in_the_reference_its_link_lands_on():
    headings = set(re.findall(r"^### (\S+)$", REFERENCE.read_text(encoding="utf-8"), re.MULTILINE))
    assert headings == set(RULES)
    assert RULES["unindexed_foreign_key"].docs == f"{DOCS_URL}#unindexed_foreign_key"


def test_the_reference_says_what_each_rule_reads():
    text = REFERENCE.read_text(encoding="utf-8")
    for rule in RULES.values():
        entry = text.split(f"### {rule.id}\n", 1)[1].split("\n### ", 1)[0]
        assert f"Reads: {rule.needs.value}" in entry, rule.id


def test_only_the_checks_that_read_the_catalog_alone_are_static():
    static = {name for name in CHECK_NAMES if is_static_check(name)}
    assert static == {index.INVALID_INDEXES, index.DUPLICATE_INDEXES, constraint.CONSTRAINT_HEALTH}
    assert not is_static_check(connection.CONNECTION_HEALTH)
    assert not is_static_check("A check nobody wrote")


def test_rules_that_sample_statistics_are_not_static():
    assert RULES["nullable_never_null"].needs is Needs.DATA
    assert RULES["unindexed_foreign_key"].static


def test_an_unknown_rule_is_refused_with_the_list():
    with pytest.raises(InvalidArgumentError, match=r"pg-debug.toml \[suppress\]: no rule is called 'naive_timestamps'"):
        parse_rule_ids(["naive_timestamps"], "pg-debug.toml [suppress]")
    assert parse_rule_ids([" naive_timestamp ", ""], "--suppress") == {"naive_timestamp"}
