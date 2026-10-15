"""Adding a partition to a partitioned table that is in use, without blocking it.

``CREATE TABLE ... PARTITION OF`` takes ACCESS EXCLUSIVE on the parent, so it
queues behind every running query on the table and everything after it queues
behind it. ``ALTER TABLE ... ATTACH PARTITION`` takes only SHARE UPDATE
EXCLUSIVE on the parent (PostgreSQL 12 and later). It still takes ACCESS
EXCLUSIVE on the table being attached and on the DEFAULT partition, and it
scans both: the new table to prove its rows fit the bound, the DEFAULT to prove
none of its rows belongs to the new partition. A validated CHECK constraint
that implies the bound lets it skip the first scan, and one on the DEFAULT that
excludes the range lets it skip the second; PostgreSQL says so at DEBUG1
("partition constraint ... is implied by existing constraints").

Both constraints are added NOT VALID, which reads nothing, and then validated,
which reads the table under SHARE UPDATE EXCLUSIVE and blocks nobody. That is
the shape ``check-migration`` reports as safe. Measured on PostgreSQL 16.
"""

from __future__ import annotations

#: PostgreSQL truncates an identifier to this many bytes.
MAX_IDENTIFIER_LENGTH = 63


def attach_partition_recipe(
    parent: str,
    partition: str,
    key: str,
    lower: str,
    upper: str,
    default: str | None = None,
) -> list[str]:
    """The statements that add one range partition without locking out the parent.

    Args:
        parent: The partitioned table, quoted as it is to be written.
        partition: The new partition's name, quoted as it is to be written.
        key: The partition key column, quoted.
        lower: The inclusive lower bound, as a SQL literal (``'2026-11-01'``).
        upper: The exclusive upper bound, as a SQL literal.
        default: The parent's DEFAULT partition, if it has one.

    Returns:
        The statements, with comments saying why each is there. Every one of
        them is something ``check-migration`` reports at low severity.
    """
    bound = _constraint_name(partition, "bound")
    within = f"{key} IS NOT NULL AND {key} >= {lower} AND {key} < {upper}"
    steps = [
        f"-- Attach rather than CREATE TABLE ... PARTITION OF, which takes ACCESS EXCLUSIVE on {parent}.",
        f"CREATE TABLE {partition} (LIKE {parent} INCLUDING ALL EXCLUDING IDENTITY);",
        f"ALTER TABLE {partition} ADD CONSTRAINT {bound} CHECK ({within}) NOT VALID;",
        f"-- load {partition} here if it is to start with rows; then:",
        f"ALTER TABLE {partition} VALIDATE CONSTRAINT {bound};  -- lets ATTACH skip scanning it",
    ]
    excludes = None
    if default is not None:
        excludes = _constraint_name(default, f"excludes_{_bare(partition)}")
        steps += [
            f"ALTER TABLE {default} ADD CONSTRAINT {excludes} CHECK (NOT ({within})) NOT VALID;",
            f"ALTER TABLE {default} VALIDATE CONSTRAINT {excludes};  -- reads {default} without blocking it; fails on rows for the new range",
        ]
    steps.append(f"ALTER TABLE {parent} ATTACH PARTITION {partition} FOR VALUES FROM ({lower}) TO ({upper});  -- SHARE UPDATE EXCLUSIVE on {parent}")
    steps.append(f"ALTER TABLE {partition} DROP CONSTRAINT {bound};  -- the partition bound enforces it now")
    if default is not None and excludes is not None:
        steps.append(f"ALTER TABLE {default} DROP CONSTRAINT {excludes};")
    return steps


def _bare(name: str) -> str:
    """The relation name without its schema or quotes."""
    return name.rpartition(".")[2].strip('"')


def _constraint_name(relation: str, suffix: str) -> str:
    """A constraint name PostgreSQL will not truncate, quoted only when it has to be."""
    name = f"{_bare(relation)}_{suffix}".encode()[:MAX_IDENTIFIER_LENGTH].decode(errors="ignore")
    if name.replace("_", "").isalnum() and name == name.lower() and not name[0].isdigit():
        return name
    return '"' + name.replace('"', '""') + '"'


__all__ = ["attach_partition_recipe"]
