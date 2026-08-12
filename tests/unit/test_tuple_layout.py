"""The shared row layout: what the SQL it builds must say."""

import pytest

from pg_debug.tuple_layout import DECLARED_ORDER
from pg_debug.tuple_layout import PACKED_ORDER
from pg_debug.tuple_layout import row_layout_ctes


def test_it_builds_the_four_ctes_the_callers_join_to():
    sql = row_layout_ctes("c.relkind = 'r'")

    for cte in ("column_facts AS (", "ordered AS (", "layout AS (", "row_shapes AS ("):
        assert cte in sql
    # The caller supplies WITH RECURSIVE and whatever follows, so the list must
    # not end with a comma.
    assert not sql.rstrip().endswith(",")


def test_the_relation_filter_is_applied():
    assert "c.relkind = 'r' AND c.relpages > 0" in row_layout_ctes("c.relkind = 'r' AND c.relpages > 0")


def test_columns_are_laid_out_in_order_rather_than_summed():
    """Summing widths ignores the padding between columns."""
    sql = row_layout_ctes("true")

    assert "ORDER BY attnum" in sql
    assert "t.typalign" in sql
    # Each column is padded forward to its own alignment boundary.
    assert "ceil(l.data_end / o.align_to) * o.align_to" in sql


def test_a_short_varlena_is_treated_as_unaligned():
    """PostgreSQL stores one with a 1-byte header and no padding."""
    sql = row_layout_ctes("true")

    assert "t.typlen = -1" in sql
    assert "THEN 1" in sql


def test_tables_with_an_unanalyzed_column_are_skipped():
    """Their width would be a guess, and the guess would look like a finding."""
    assert "HAVING bool_and(analyzed)" in row_layout_ctes("true")


def test_one_ordering_produces_one_variant():
    sql = row_layout_ctes("true")

    assert "1 AS variant" in sql
    assert "2 AS variant" not in sql
    assert "UNION ALL" in sql  # the recursion's own, not a second ordering


def test_two_orderings_are_numbered_and_laid_out_independently():
    """Variant 1 is the table as it stands, variant 2 the packed alternative."""
    sql = row_layout_ctes("true", (DECLARED_ORDER, PACKED_ORDER))

    assert f"ORDER BY {DECLARED_ORDER}) AS ord" in sql
    assert f"ORDER BY {PACKED_ORDER}) AS ord" in sql
    assert "1 AS variant" in sql
    assert "2 AS variant" in sql
    # The recursion must not step from one variant's column into another's.
    assert "o.variant = l.variant" in sql


def test_the_packed_order_puts_the_widest_alignment_first():
    """Descending alignment is what removes the padding."""
    assert PACKED_ORDER.startswith("align_to DESC")
    # ... and variable-width columns after fixed-width ones of that alignment.
    assert "fixed_width DESC" in PACKED_ORDER


def test_at_least_one_ordering_is_required():
    with pytest.raises(ValueError):
        row_layout_ctes("true", ())
