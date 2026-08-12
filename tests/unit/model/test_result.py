"""The vocabulary: rule selection, ordering and serialization."""

import pytest

from pg_debug.errors import InvalidArgumentError
from pg_debug.model import Confidence
from pg_debug.model import ModelFinding
from pg_debug.model import RuleId
from pg_debug.model import Severity


def finding(**overrides) -> ModelFinding:
    defaults = dict(
        rule=RuleId.MISSING_PRIMARY_KEY,
        confidence=Confidence.PROVEN,
        severity=Severity.HIGH,
        schema="public",
        table="orders",
        columns=[],
        summary="summary",
        detail="detail",
    )
    return ModelFinding(**{**defaults, **overrides})


# -- Rule selection ---------------------------------------------------------


def test_all_expands_to_every_rule_except_itself():
    rules = RuleId.parse("all")

    assert RuleId.ALL not in rules
    assert rules == {r for r in RuleId if r is not RuleId.ALL}


def test_a_comma_separated_string_selects_several():
    assert RuleId.parse("missing_primary_key,naive_timestamp") == {
        RuleId.MISSING_PRIMARY_KEY,
        RuleId.NAIVE_TIMESTAMP,
    }


def test_an_iterable_of_enum_members_is_accepted():
    assert RuleId.parse([RuleId.NAIVE_TIMESTAMP]) == {RuleId.NAIVE_TIMESTAMP}


def test_an_unknown_rule_names_the_valid_ones():
    with pytest.raises(InvalidArgumentError) as excinfo:
        RuleId.parse("no_such_rule")

    assert "unindexed_foreign_key" in str(excinfo.value)


def test_selecting_nothing_is_an_error():
    with pytest.raises(InvalidArgumentError):
        RuleId.parse(" ")


# -- Ordering ---------------------------------------------------------------


def test_severity_orders_before_impact():
    """A large low-severity saving does not outrank a high-severity defect."""
    high = finding(severity=Severity.HIGH, rank=1.0)
    low = finding(severity=Severity.LOW, rank=1_000_000.0)

    assert sorted([low, high], key=lambda f: f.sort_key) == [high, low]


def test_within_a_severity_the_larger_impact_comes_first():
    small = finding(table="small", rank=10.0)
    large = finding(table="large", rank=10_000.0)

    assert sorted([small, large], key=lambda f: f.sort_key) == [large, small]


# -- Serialization ----------------------------------------------------------


def test_a_proven_finding_carries_no_verify_query():
    """There is nothing to confirm about a fact read from the catalog."""
    assert "verify" not in finding().to_dict()


def test_a_candidate_serializes_the_query_that_settles_it():
    result = finding(
        confidence=Confidence.CANDIDATE,
        verify="SELECT count(*) FROM public.orders WHERE ref IS NULL;",
    ).to_dict()

    assert result["confidence"] == "candidate"
    assert result["verify"] == "SELECT count(*) FROM public.orders WHERE ref IS NULL;"


def test_the_internal_rank_is_not_serialized():
    """It orders findings within one rule and means nothing across rules."""
    assert "rank" not in finding(rank=5.0).to_dict()


def test_impact_bytes_appears_only_where_the_cost_is_a_size():
    assert "impact_bytes" not in finding().to_dict()
    assert finding(impact_bytes=4096).to_dict()["impact_bytes"] == 4096


def test_the_qualified_table_is_written_as_a_statement_would():
    assert finding(schema="reporting", table="orders").qualified_table == "reporting.orders"
