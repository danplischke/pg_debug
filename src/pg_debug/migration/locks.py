"""What each kind of DDL locks, for how long, and whether it rewrites.

Every entry here was measured against PostgreSQL 16 rather than recalled: each
statement was run inside a transaction, ``pg_locks`` read for the modes it held
on the target table, and ``pg_class.relfilenode`` compared before and after to
see whether the heap was rewritten. Several results contradict the usual
advice — ``ADD COLUMN ... NOT NULL DEFAULT 0`` has not rewritten the table
since PostgreSQL 11, and ``ADD FOREIGN KEY`` takes SHARE ROW EXCLUSIVE rather
than ACCESS EXCLUSIVE, so it never blocks readers.

The lock level alone does not tell you whether a statement is safe. What
matters is the lock level *and* how long it is held: ACCESS EXCLUSIVE taken for
a catalog update lasts microseconds and is unremarkable, while the same lock
taken for a rewrite of a large table is an outage. That is what
:class:`Duration` records.
"""

from __future__ import annotations

from enum import Enum


class LockLevel(str, Enum):
    """The lock a statement takes on its table, in PostgreSQL's own names."""

    ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"
    SHARE_ROW_EXCLUSIVE = "SHARE ROW EXCLUSIVE"
    SHARE = "SHARE"
    SHARE_UPDATE_EXCLUSIVE = "SHARE UPDATE EXCLUSIVE"

    @property
    def blocks(self) -> str:
        """What an application cannot do while this lock is held."""
        return _BLOCKS[self]


_BLOCKS = {
    LockLevel.ACCESS_EXCLUSIVE: "every read and write, including SELECT",
    LockLevel.SHARE_ROW_EXCLUSIVE: "writes; reads continue",
    LockLevel.SHARE: "writes; reads continue",
    LockLevel.SHARE_UPDATE_EXCLUSIVE: "nothing an application does; only other DDL and VACUUM",
}


class Duration(str, Enum):
    """How long the lock is held — the half of the story a lock level omits."""

    #: A catalog update. The same however large the table is.
    INSTANT = "instant"
    #: The table is read through once under the lock.
    SCAN = "proportional to the table, one pass"
    #: The table is written out again under the lock, and needs room for both
    #: copies at once.
    REWRITE = "proportional to the table, and needs its size again in free space"


#: The PostgreSQL version the measurements above were taken against.
MEASURED_ON = 16


__all__ = ["MEASURED_ON", "Duration", "LockLevel"]
