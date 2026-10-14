"""Which managed platform a server runs on, and what that detection rests on.

A few findings mean something else on a managed platform: Neon runs every
compute with ``fsync = off`` and is no less durable for it, and the way to get
HypoPG depends on whether the platform offers it at all. So pg-debug asks,
from two places. The connection URL's host is under a domain the platform
owns, or its options carry one only the platform takes. The server has
settings, functions and roles the platform's own build adds, read with one
statement that restricted mode allows.

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
import re
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
    LAKEBASE = "lakebase"
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
    Platform.LAKEBASE: "Databricks Lakebase",
    Platform.UNKNOWN: "an unrecognised server",
}


class Basis(str, Enum):
    """The kind of signal a detection rests on."""

    #: The connection URL: a host under a domain the platform names its
    #: hosts in, or an option only the platform takes.
    URL = "url"
    #: A setting or function the platform's own build adds.
    SERVER = "server"
    #: A role the platform creates. A role is a catalog row: any superuser
    #: can create one under any name, and a dump of a platform's roles
    #: carries them to wherever it is restored.
    ROLE = "role"
    #: Nothing matched, or nothing matched on its own.
    NONE = "none"


@dataclass(frozen=True)
class PlatformDetection:
    """The platform, and the signal it was recognised by.

    Attributes:
        platform: What the server was taken for.
        signal: What that rests on, in a sentence a finding can quote; for
            :attr:`Platform.UNKNOWN`, why nothing matched.
        basis: The kind of signal that is. A finding whose verdict turns on
            the platform can ask for more than a role, as Neon's ``fsync``
            does.
    """

    platform: Platform
    signal: str
    basis: Basis

    @property
    def known(self) -> bool:
        return self.platform is not Platform.UNKNOWN

    @property
    def firm(self) -> bool:
        """Whether the platform rests on more than a role: on the URL, or on the server's own settings and functions.

        A role is the one signal a server elsewhere carries by accident, from
        a restored dump of the platform's roles.
        """
        return self.known and self.basis in (Basis.URL, Basis.SERVER)


#: Host suffixes under a domain only the platform can name hosts in. Aurora
#: DSQL's and Databricks Lakebase's are matched separately: what names them is
#: a label in the middle or at the start of the host.
_HOST_SUFFIXES: tuple[tuple[str, Platform], ...] = (
    ("neon.tech", Platform.NEON),
    ("supabase.co", Platform.SUPABASE),
    ("pooler.supabase.com", Platform.SUPABASE),
    ("horizon.psdb.cloud", Platform.PLANETSCALE),
    ("db.postgresbridge.com", Platform.CRUNCHY_BRIDGE),
    ("db.prisma.io", Platform.PRISMA_POSTGRES),
    ("tsdb.cloud.timescale.com", Platform.TIGER_CLOUD),
)

#: The domains Aurora DSQL's endpoints are under: on.aws for a cluster's own
#: endpoint and for one reached through PrivateLink with private DNS, and
#: vpce.amazonaws.com for a PrivateLink endpoint's own name.
_DSQL_DOMAINS = ("on.aws", "vpce.amazonaws.com")

#: The label that names DSQL in those hosts: ``dsql`` in a cluster's own
#: endpoint, ``<cluster-id>.dsql.<region>.on.aws``, and the PrivateLink
#: service's identifier, such as ``dsql-fnh4``, in
#: ``<cluster-id>.dsql-fnh4.<region>.on.aws`` and
#: ``vpce-<id>.dsql-fnh4.<region>.vpce.amazonaws.com``.
_DSQL_LABEL = re.compile(r"dsql(?:-[a-z0-9]+)?")

#: The option DSQL reads the cluster from when the host is a PrivateLink
#: endpoint's own name or address: ``-c amzn-cluster-id=<id>``. PostgreSQL
#: reads dashes in an option's name as underscores, so either is the option.
_DSQL_CLUSTER_OPTION = re.compile(r"(?:^|\s)(?:-c\s*|--)amzn[-_]cluster[-_]id=")

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

#: Platforms the URL decides before the server is asked. Aurora DSQL is not
#: PostgreSQL underneath, and the probe is not known to work there.
#: Databricks Lakebase is built on Neon, and its server may carry Neon's
#: settings, which, asked first, would name the wrong platform.
_DECIDED_BY_URL = frozenset({Platform.AURORA_DSQL, Platform.LAKEBASE})

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


def options_in(url: str | None) -> str:
    """The server options a connection URL or conninfo string sends.

    libpq falls back to ``PGOPTIONS`` when the string sets none, so that is
    read too. A string psycopg cannot parse sends none.
    """
    if not url:
        return ""
    try:
        options = conninfo_to_dict(url).get("options") or os.environ.get("PGOPTIONS") or ""
    except psycopg.Error:
        return ""
    return str(options)


def dsql_label(host: str) -> str | None:
    """The label that makes a host an Aurora DSQL endpoint, or None when it is not one.

    The label has a name in front of it, the cluster's or the PrivateLink
    endpoint's, and one of the domains DSQL's endpoints are under after it.
    """
    for domain in _DSQL_DOMAINS:
        if host.endswith("." + domain):
            for label in host.removesuffix("." + domain).split(".")[1:]:
                if _DSQL_LABEL.fullmatch(label):
                    return label
    return None


def is_dsql_host(host: str) -> bool:
    """Whether a host is an Aurora DSQL endpoint; :func:`dsql_label` says which forms are."""
    return dsql_label(host) is not None


def is_lakebase_host(host: str) -> bool:
    """Whether a host is a Databricks Lakebase endpoint.

    Two forms are documented: a database instance's,
    ``instance-<id>.database.cloud.databricks.com``, and a compute's, whose
    first label is its ``ep-`` identifier under databricks.com, as in
    ``ep-abc-123.databricks.com``.
    """
    if host.endswith(".database.cloud.databricks.com"):
        return True
    return host.endswith(".databricks.com") and host.split(".", 1)[0].startswith("ep-")


def detect_from_url(url: str | None) -> PlatformDetection | None:
    """The platform the URL's hosts or options name, or None when they name none pg-debug knows."""
    hosts = hosts_in(url)
    for host in hosts:
        label = dsql_label(host)
        if label is not None:
            domain = next(domain for domain in _DSQL_DOMAINS if host.endswith("." + domain))
            return PlatformDetection(Platform.AURORA_DSQL, f"the host {host} has the label {label} under {domain}", Basis.URL)
    if _DSQL_CLUSTER_OPTION.search(options_in(url)):
        return PlatformDetection(
            Platform.AURORA_DSQL,
            "the connection's options set amzn-cluster-id, the option Aurora DSQL takes the cluster from",
            Basis.URL,
        )
    for host in hosts:
        if is_lakebase_host(host):
            return PlatformDetection(Platform.LAKEBASE, f"the host {host} has the form of a Databricks Lakebase endpoint", Basis.URL)
        for suffix, platform in _HOST_SUFFIXES:
            if host == suffix or host.endswith("." + suffix):
                return PlatformDetection(platform, f"the host {host} is under {suffix}", Basis.URL)
    return None


async def detect_platform(sql_driver: SqlDriver, url: str | None = None) -> PlatformDetection:
    """Say which managed platform the server runs on, and from what.

    The signals are read in the order they are trusted: the server's own
    settings and functions, then the URL's hosts and options, then the
    platform's roles. Aurora DSQL and Databricks Lakebase are the exceptions,
    decided from the URL before anything is run: DSQL is not PostgreSQL
    underneath and the probe is not known to work there, and Lakebase is
    built on Neon and may carry Neon's settings. A probe the server refuses
    leaves the platform unknown rather than failing the command that asked:
    no command depends on knowing it.

    Args:
        sql_driver: The driver to read the server's signals through. A
            restricted one will do.
        url: The connection URL, for its hosts and options. Without it only
            the server is asked, and an unknown platform's reason says that
            no URL was read.
    """
    from_url = detect_from_url(url)
    if from_url is not None and from_url.platform in _DECIDED_BY_URL:
        return from_url
    url_said = "the connection URL names no platform pg-debug knows" if url else "pg-debug had no connection URL to read a host from"

    try:
        rows = await sql_driver.execute_query(PLATFORM_SQL)
    except QueryError as e:
        logger.info("Could not read the platform's signals from the server: %s", e)
        if from_url is not None:
            return from_url
        return PlatformDetection(Platform.UNKNOWN, f"the server refused the platform probe ({e}), and {url_said}", Basis.NONE)

    cells = rows[0].cells if rows else {}
    for column, platform, signal in _SERVER_SIGNALS:
        # A strict test: these columns are booleans, and anything else is no answer.
        if cells.get(column) is True:
            return PlatformDetection(platform, signal, Basis.SERVER)

    if from_url is not None:
        return from_url

    roles = cells.get("platform_roles")
    found = [role for role in roles if role in PLATFORM_ROLES] if isinstance(roles, list) else []
    platforms = {PLATFORM_ROLES[role] for role in found}
    if len(platforms) == 1:
        signal = f"the role {found[0]} exists" if len(found) == 1 else f"the roles {', '.join(found)} exist"
        return PlatformDetection(platforms.pop(), signal, Basis.ROLE)
    if platforms:
        return PlatformDetection(
            Platform.UNKNOWN, f"roles of more than one platform exist ({', '.join(found)}), and nothing else names one", Basis.NONE
        )

    return PlatformDetection(
        Platform.UNKNOWN,
        f"none of the signals pg-debug knows matched: not the server's settings, functions or roles, and {url_said}",
        Basis.NONE,
    )


__all__ = [
    "PLATFORM_ROLES",
    "PLATFORM_SQL",
    "Basis",
    "Platform",
    "PlatformDetection",
    "detect_from_url",
    "detect_platform",
    "dsql_label",
    "hosts_in",
    "is_dsql_host",
    "is_lakebase_host",
    "options_in",
]
