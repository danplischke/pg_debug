"""Which managed platform a server runs on, and what that detection rests on.

A few findings mean something else on a managed platform: Neon runs every
compute with ``fsync = off`` and is no less durable for it, and the way to get
HypoPG depends on whether the platform offers it at all. So pg-debug asks,
from two places. The connection URL's host is under a domain the platform
owns. The server has settings, functions and roles the platform's own build
adds, read with one statement that restricted mode allows.

None of these signals has been tried on a live platform: each comes from the
platform's documentation or source code. So a detection says which signal it
rests on, for a reader to check, and a server none of them matches is
:attr:`Platform.UNKNOWN`. That is not "self-hosted": a managed platform whose
signals differ from the documented ones, or that is reached through a tunnel
or a private endpoint, reads the same.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .errors import QueryError

if TYPE_CHECKING:
    from .db.driver import SqlDriver

logger = logging.getLogger(__name__)


class Platform(str, Enum):
    """A managed platform pg-debug knows how to recognise."""

    NEON = "neon"
    SUPABASE = "supabase"
    RDS = "rds"
    AURORA = "aurora"
    AURORA_DSQL = "aurora_dsql"
    CLOUD_SQL = "cloud_sql"
    ALLOYDB = "alloydb"
    AZURE = "azure"
    PLANETSCALE = "planetscale"
    CRUNCHY_BRIDGE = "crunchy_bridge"
    PRISMA_POSTGRES = "prisma_postgres"
    TIGER_CLOUD = "tiger_cloud"
    #: None of the signals matched. Not the same as self-hosted.
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        """The platform's name, as a sentence would use it."""
        return _LABELS[self]

    @property
    def refuses_statement_timeout(self) -> bool:
        """Whether the server refuses ``statement_timeout`` at any scope.

        Aurora DSQL does, by its documentation; pg-debug has not been run
        against it.
        """
        return self is Platform.AURORA_DSQL


_LABELS = {
    Platform.NEON: "Neon",
    Platform.SUPABASE: "Supabase",
    Platform.RDS: "Amazon RDS",
    Platform.AURORA: "Amazon Aurora",
    Platform.AURORA_DSQL: "Aurora DSQL",
    Platform.CLOUD_SQL: "Cloud SQL",
    Platform.ALLOYDB: "AlloyDB",
    Platform.AZURE: "Azure Database for PostgreSQL",
    Platform.PLANETSCALE: "PlanetScale",
    Platform.CRUNCHY_BRIDGE: "Crunchy Bridge",
    Platform.PRISMA_POSTGRES: "Prisma Postgres",
    Platform.TIGER_CLOUD: "Tiger Cloud",
    Platform.UNKNOWN: "an unrecognised server",
}


@dataclass(frozen=True)
class PlatformDetection:
    """The platform, and the signal it was recognised by.

    Attributes:
        platform: What the server was taken for.
        signal: What that rests on, in a sentence a finding can quote; for
            :attr:`Platform.UNKNOWN`, why nothing matched.
    """

    platform: Platform
    signal: str

    @property
    def known(self) -> bool:
        return self.platform is not Platform.UNKNOWN


#: Host suffixes under a domain only the platform can name hosts in. Aurora
#: DSQL's are matched separately: its region sits between two fixed parts.
_HOST_SUFFIXES: tuple[tuple[str, Platform], ...] = (
    ("neon.tech", Platform.NEON),
    ("supabase.co", Platform.SUPABASE),
    ("pooler.supabase.com", Platform.SUPABASE),
    ("horizon.psdb.cloud", Platform.PLANETSCALE),
    ("db.postgresbridge.com", Platform.CRUNCHY_BRIDGE),
    ("db.prisma.io", Platform.PRISMA_POSTGRES),
    ("tsdb.cloud.timescale.com", Platform.TIGER_CLOUD),
)

#: Roles a platform creates in every database cluster it runs. A role is a
#: catalog row any superuser can create under any name, so these are the
#: weakest of the signals and are read last.
PLATFORM_ROLES: dict[str, Platform] = {
    "neon_superuser": Platform.NEON,
    "rds_superuser": Platform.RDS,
    "pscale_superuser": Platform.PLANETSCALE,
    "cloudsqlsuperuser": Platform.CLOUD_SQL,
    "alloydbsuperuser": Platform.ALLOYDB,
    "azure_pg_admin": Platform.AZURE,
    "tsdbadmin": Platform.TIGER_CLOUD,
}

#: Settings and functions the platform's own build or extensions add, in the
#: order they are trusted. Aurora comes before RDS because Aurora has the RDS
#: settings too.
_SERVER_SIGNALS: tuple[tuple[str, Platform, str], ...] = (
    ("neon_timeline_id", Platform.NEON, "the server has the setting neon.timeline_id"),
    ("supautils_privileged_role", Platform.SUPABASE, "the server has the setting supautils.privileged_role"),
    ("aurora_version", Platform.AURORA, "the server has the function aurora_version()"),
    ("rds_extensions", Platform.RDS, "the server has the setting rds.extensions"),
    ("azure_extensions", Platform.AZURE, "the server has the setting azure.extensions"),
)

#: Every name here is allowed in restricted mode. A setting that does not
#: exist reads NULL with current_setting's second argument, rather than an
#: error, and to_regproc returns NULL for a function that does not.
PLATFORM_SQL = """
SELECT
    current_setting('neon.timeline_id', true) IS NOT NULL AS neon_timeline_id,
    current_setting('supautils.privileged_role', true) IS NOT NULL AS supautils_privileged_role,
    to_regproc('aurora_version') IS NOT NULL AS aurora_version,
    current_setting('rds.extensions', true) IS NOT NULL AS rds_extensions,
    current_setting('azure.extensions', true) IS NOT NULL AS azure_extensions,
    ARRAY(
        SELECT rolname::text
        FROM pg_roles
        WHERE rolname IN (
            'neon_superuser', 'rds_superuser', 'pscale_superuser', 'cloudsqlsuperuser',
            'alloydbsuperuser', 'azure_pg_admin', 'tsdbadmin'
        )
        ORDER BY rolname
    ) AS platform_roles
"""


def hosts_in(url: str | None) -> list[str]:
    """The hosts a connection URL or conninfo string names, lower case.

    libpq takes a comma-separated list, and falls back to ``PGHOST`` when the
    string names none, so both are read. A string psycopg cannot parse names
    no host; connecting will fail with its own reason.
    """
    if not url:
        return []
    try:
        host = conninfo_to_dict(url).get("host") or os.environ.get("PGHOST") or ""
    except psycopg.Error:
        return []
    return [part.strip().rstrip(".").lower() for part in str(host).split(",") if part.strip()]


def is_dsql_host(host: str) -> bool:
    """Whether a host is an Aurora DSQL cluster endpoint, ``<id>.dsql.<region>.on.aws``."""
    return ".dsql." in host and host.endswith(".on.aws")


def detect_from_url(url: str | None) -> PlatformDetection | None:
    """The platform the URL's host names, or None when it names none pg-debug knows."""
    for host in hosts_in(url):
        if is_dsql_host(host):
            return PlatformDetection(Platform.AURORA_DSQL, f"the host {host} has .dsql. in a name under on.aws")
        for suffix, platform in _HOST_SUFFIXES:
            if host == suffix or host.endswith("." + suffix):
                return PlatformDetection(platform, f"the host {host} is under {suffix}")
    return None


async def detect_platform(sql_driver: SqlDriver, url: str | None = None) -> PlatformDetection:
    """Say which managed platform the server runs on, and from what.

    The signals are read in the order they are trusted: the server's own
    settings and functions, then the URL's host, then the platform's roles.
    An Aurora DSQL host is the exception, decided before anything is run,
    because DSQL is not PostgreSQL underneath and the probe is not known to
    work there. A probe the server refuses leaves the platform unknown rather
    than failing the command that asked: no command depends on knowing it.

    Args:
        sql_driver: The driver to read the server's signals through. A
            restricted one will do.
        url: The connection URL, for its host. Without it only the server is
            asked.
    """
    from_url = detect_from_url(url)
    if from_url is not None and from_url.platform is Platform.AURORA_DSQL:
        return from_url

    try:
        rows = await sql_driver.execute_query(PLATFORM_SQL)
    except QueryError as e:
        logger.info("Could not read the platform's signals from the server: %s", e)
        if from_url is not None:
            return from_url
        return PlatformDetection(Platform.UNKNOWN, f"the server refused the platform probe ({e}), and the host names no platform pg-debug knows")

    cells = rows[0].cells if rows else {}
    for column, platform, signal in _SERVER_SIGNALS:
        # A strict test: these columns are booleans, and anything else is no answer.
        if cells.get(column) is True:
            return PlatformDetection(platform, signal)

    if from_url is not None:
        return from_url

    roles = cells.get("platform_roles")
    found = [role for role in roles if role in PLATFORM_ROLES] if isinstance(roles, list) else []
    platforms = {PLATFORM_ROLES[role] for role in found}
    if len(platforms) == 1:
        return PlatformDetection(platforms.pop(), f"the role {found[0]} exists" if len(found) == 1 else f"the roles {', '.join(found)} exist")
    if platforms:
        return PlatformDetection(Platform.UNKNOWN, f"roles of more than one platform exist ({', '.join(found)}), and nothing else names one")

    return PlatformDetection(
        Platform.UNKNOWN,
        "none of the signals pg-debug knows matched: not the host, nor the server's settings, functions or roles",
    )


__all__ = [
    "PLATFORM_ROLES",
    "PLATFORM_SQL",
    "Platform",
    "PlatformDetection",
    "detect_from_url",
    "detect_platform",
    "hosts_in",
    "is_dsql_host",
]
