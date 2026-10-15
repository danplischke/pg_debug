"""How far a column fed by a sequence can count before it runs out.

Two things judge this and must not disagree: the sequence health check, which
looks at every column with a ``nextval`` default, and the data model review,
which looks at primary keys and reads identity columns too. Both get the limit
of a column's type from here.
"""

from __future__ import annotations

SMALLINT_MAX = 2**15 - 1
INTEGER_MAX = 2**31 - 1
BIGINT_MAX = 2**63 - 1

#: The largest value each integer type can hold, by the name ``format_type``
#: gives it.
INTEGER_TYPE_MAX = {
    "smallint": SMALLINT_MAX,
    "integer": INTEGER_MAX,
    "bigint": BIGINT_MAX,
}


def type_max(column_type: str) -> int:
    """The largest value a column of this type holds.

    Anything that is not ``smallint`` or ``integer`` is judged as ``bigint``,
    which is the widest a sequence counts.
    """
    return INTEGER_TYPE_MAX.get(column_type, BIGINT_MAX)


__all__ = ["BIGINT_MAX", "INTEGER_MAX", "INTEGER_TYPE_MAX", "SMALLINT_MAX", "type_max"]
