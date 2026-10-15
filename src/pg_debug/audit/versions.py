"""What pg-debug knew about PostgreSQL releases when it was built.

A server cannot say whether a newer release of its own version exists; that
is knowledge from outside it, and it ages. So it is kept here, with the date
it was last checked against https://www.postgresql.org/support/versioning/,
and every finding that rests on it says that date. A pg-debug older than the
latest release will call a server current that is not: the date is how a
reader knows to discount it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: When the table below was last checked against postgresql.org.
KNOWN_AS_OF = date(2026, 10, 14)

#: The date of the newest minor releases in the table (18.6, 17.11, 16.15,
#: 15.19 and 14.24 were released together).
LATEST_MINOR_RELEASE_DATE = date(2026, 8, 13)

#: The newest major version released as of :data:`KNOWN_AS_OF`. 19 was in
#: beta.
NEWEST_MAJOR = 18


@dataclass(frozen=True)
class MajorRelease:
    """A major version, its newest minor release, and the end of its support."""

    major: int
    latest_minor: int
    final_release: date

    @property
    def latest(self) -> str:
        return f"{self.major}.{self.latest_minor}"


#: From the versioning policy page. A final release date in the future is the
#: date the project has announced, not one that has happened.
RELEASES: dict[int, MajorRelease] = {
    release.major: release
    for release in (
        MajorRelease(18, 6, date(2030, 11, 14)),
        MajorRelease(17, 11, date(2029, 11, 8)),
        MajorRelease(16, 15, date(2028, 11, 9)),
        MajorRelease(15, 19, date(2027, 11, 11)),
        MajorRelease(14, 24, date(2026, 11, 12)),
        MajorRelease(13, 23, date(2025, 11, 13)),
        MajorRelease(12, 22, date(2024, 11, 21)),
        MajorRelease(11, 22, date(2023, 11, 9)),
        MajorRelease(10, 23, date(2022, 11, 10)),
    )
}


def split_version(server_version_num: int) -> tuple[int, int]:
    """``160004`` is ``(16, 4)``. From 10 on, the last two digits are the minor."""
    return server_version_num // 10000, server_version_num % 100


__all__ = ["KNOWN_AS_OF", "LATEST_MINOR_RELEASE_DATE", "NEWEST_MAJOR", "RELEASES", "MajorRelease", "split_version"]
