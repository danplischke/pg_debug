"""Utilities for working with PostgreSQL extensions."""

import logging
import weakref
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..errors import QueryError
from ..platform import Platform
from ..platform import PlatformDetection
from ..platform import detect_platform
from .driver import DbConnPool
from .driver import SqlDriver
from .restricted import SafeSqlDriver

logger = logging.getLogger(__name__)

#: Platforms whose documentation offers HypoPG for the user to create. Azure
#: offers it too, once it is allowlisted, and gets messages of its own.
_OFFER_HYPOPG = frozenset(
    {
        Platform.NEON,
        Platform.SUPABASE,
        Platform.RDS,
        Platform.AURORA,
        Platform.ALLOYDB,
        Platform.PLANETSCALE,
        Platform.CRUNCHY_BRIDGE,
        Platform.AZURE,
    }
)

#: Platforms whose documentation says they do not offer it.
_LACK_HYPOPG = frozenset({Platform.CLOUD_SQL, Platform.TIGER_CLOUD, Platform.PRISMA_POSTGRES, Platform.AURORA_DSQL, Platform.LAKEBASE})

#: The statement that creates it.
_CREATE_HYPOPG = "CREATE EXTENSION hypopg"

#: The major version of each server asked, kept for as long as the pool or the
#: connection that reached it. One process can hold clients of servers of
#: different versions, and a version kept once for the process sent the
#: second of them to the first one's catalog views: PostgreSQL 17's
#: pg_stat_checkpointer to a 15. A pool that reconnects opens a new psycopg
#: pool, so a server upgraded or failed over behind the same URL is asked
#: again rather than remembered.
_server_versions: weakref.WeakKeyDictionary[object, int] = weakref.WeakKeyDictionary()


@dataclass
class ExtensionStatus:
    """Status of an extension."""

    is_installed: bool
    is_available: bool
    name: str
    message: str
    default_version: str | None


def reset_postgres_version_cache() -> None:
    """Forget the version of every server asked so far. Primarily used for testing."""
    _server_versions.clear()


def _server_of(sql_driver: SqlDriver) -> object | None:
    """What stands for the server a driver reaches: its open pool, or the connection it was given.

    Restricted mode wraps the driver that holds the connection, and a driver
    made by ``with_context`` shares it, so all of them share one entry. None
    while the driver's pool is not open yet; the query for the version opens
    it, and the version is kept under the pool it opened.
    """
    while isinstance(sql_driver, SafeSqlDriver):
        sql_driver = sql_driver.sql_driver
    conn = getattr(sql_driver, "conn", None)
    if isinstance(conn, DbConnPool):
        return conn.pool
    return conn


def _kept_version(server: object | None) -> int | None:
    """The version kept for a server, or None when none is or none can be."""
    if server is None:
        return None
    try:
        return _server_versions.get(server)
    except TypeError:
        # A connection object that cannot be hashed or weakly referenced:
        # nothing can have been kept for it.
        return None


def _keep_version(server: object | None, version: int) -> None:
    """Keep a server's version, unless its connection object cannot be a weak key.

    Such an object, a dataclass or a class with ``__slots__`` wrapping a
    connection, has its server asked again each time rather than make every
    version-gated feature fail on a cache.
    """
    if server is None:
        return
    try:
        _server_versions[server] = version
    except TypeError:
        logger.debug("Not keeping the server version for %r, which cannot be weakly referenced or hashed", type(server).__name__)


async def get_postgres_version(sql_driver: SqlDriver) -> int:
    """
    Get the major PostgreSQL version as an integer.

    Asked once per server, and kept for as long as the pool or connection that
    reached it.

    Args:
        sql_driver: An instance of SqlDriver to execute queries

    Returns:
        The major PostgreSQL version as an integer (e.g., 16 for PostgreSQL 16.2)
        Returns 0 if the version cannot be determined
    """
    kept = _kept_version(_server_of(sql_driver))
    if kept is not None:
        return kept

    try:
        # server_version_num, not server_version: the number is the same for
        # a release and its pre-releases (180000 for 18beta1 and 18rc1), where
        # the text is not a number before its first dot, and a packager's
        # suffix such as "(Debian 18~rc1-1.pgdg120+1)" is never in it.
        rows = await sql_driver.execute_query("SHOW server_version_num")
        if not rows:
            logger.warning("Could not determine PostgreSQL version")
            return 0

        # 160004 is 16.4; 90624 is 9.6.24, whose major version is reported as 9.
        version = int(rows[0].cells["server_version_num"]) // 10000
    except Exception as e:
        raise QueryError(f"Error determining PostgreSQL version: {e}") from e

    # Read again: the query may have been what opened the pool.
    _keep_version(_server_of(sql_driver), version)
    return version


async def check_postgres_version_requirement(sql_driver: SqlDriver, min_version: int, feature_name: str) -> tuple[bool, str]:
    """
    Check if the PostgreSQL version meets the minimum requirement.

    Args:
        sql_driver: An instance of SqlDriver to execute queries
        min_version: The minimum required PostgreSQL version
        feature_name: Name of the feature that requires this version

    Returns:
        A tuple of (meets_requirement, message)
    """
    pg_version = await get_postgres_version(sql_driver)

    if pg_version >= min_version:
        return True, f"PostgreSQL version {pg_version} meets the requirement for {feature_name}"

    return False, (
        f"This feature ({feature_name}) requires PostgreSQL {min_version} or later. Your current version is PostgreSQL {pg_version or 'unknown'}."
    )


async def check_extension(
    sql_driver: SqlDriver,
    extension_name: str,
    include_messages: bool = True,
    message_type: Literal["plain", "markdown"] = "plain",
) -> ExtensionStatus:
    """
    Check if a PostgreSQL extension is installed or available.

    Args:
        sql_driver: An instance of SqlDriver to execute queries
        extension_name: Name of the extension to check
        include_messages: Whether to include user-friendly messages in the result
        message_type: Format for messages - 'plain' or 'markdown'

    Returns:
        ExtensionStatus with fields:
            - is_installed: True if the extension is installed
            - is_available: True if the extension is available but not installed
            - name: The extension name
            - message: A user-friendly message about the extension status
            - default_version: The default version of the extension if available
    """
    # Check if the extension is installed
    installed_result = await SafeSqlDriver.execute_param_query(
        sql_driver,
        "SELECT extversion FROM pg_extension WHERE extname = {}",
        [extension_name],
    )

    # Initialize result
    result = ExtensionStatus(
        is_installed=False,
        is_available=False,
        name=extension_name,
        message="",
        default_version=None,
    )

    if installed_result and len(installed_result) > 0:
        # Extension is installed
        version = installed_result[0].cells.get("extversion", "unknown")
        result.is_installed = True
        result.is_available = True

        if include_messages:
            if message_type == "markdown":
                result.message = f"The **{extension_name}** extension (version {version}) is already installed."
            else:
                result.message = f"The {extension_name} extension (version {version}) is already installed."
    else:
        # Check if the extension is available but not installed
        available_result = await SafeSqlDriver.execute_param_query(
            sql_driver,
            "SELECT default_version FROM pg_available_extensions WHERE name = {}",
            [extension_name],
        )

        if available_result and len(available_result) > 0:
            # Extension is available but not installed
            result.is_available = True
            result.default_version = available_result[0].cells.get("default_version")

            if include_messages:
                if message_type == "markdown":
                    result.message = (
                        f"The **{extension_name}** extension is available but not installed.\n\n"
                        f"You can install it by running: `CREATE EXTENSION {extension_name};`."
                    )
                else:
                    result.message = (
                        f"The {extension_name} extension is available but not installed.\n"
                        f"You can install it by running: CREATE EXTENSION {extension_name};"
                    )
        else:
            # Extension is not available
            if include_messages:
                if message_type == "markdown":
                    result.message = (
                        f"The **{extension_name}** extension is not available on this PostgreSQL server.\n\n"
                        f"To install it, you need to:\n"
                        f"1. Install the extension package on the server\n"
                        f"2. Run: `CREATE EXTENSION {extension_name};`"
                    )
                else:
                    result.message = (
                        f"The {extension_name} extension is not available on this PostgreSQL server.\n"
                        f"To install it, you need to:\n"
                        f"1. Install the extension package on the server\n"
                        f"2. Run: CREATE EXTENSION {extension_name};"
                    )

    return result


async def check_hypopg_installation_status(
    sql_driver: SqlDriver,
    message_type: Literal["plain", "markdown"] = "markdown",
    platform: Callable[[], Awaitable[PlatformDetection]] | None = None,
) -> tuple[bool, str]:
    """
    Say whether HypoPG is installed, and where it is not, how to get it here.

    How depends on where the server runs. Most managed platforms offer HypoPG
    for the user to create, Azure only once it is allowlisted, and a few do not
    offer it at all; telling a user on one of those to install a package with
    apt-get sends them after something the platform does not let them do. So
    the platform is asked for, but only once HypoPG turns out to be missing:
    an installed one needs no advice.

    Args:
        sql_driver: An instance of SqlDriver to execute queries
        message_type: Format for messages - 'plain' or 'markdown'
        platform: A coroutine function that says which platform the server
            runs on, awaited only when HypoPG is missing. The client passes
            its own, which also reads the connection URL's host and detects
            once per client. Without it the server's own signals are read.

    Returns:
        Whether HypoPG is installed, and a message that says so, or says how
        to get it.
    """
    status = await check_extension(sql_driver, "hypopg", include_messages=False)

    if status.is_installed:
        if message_type == "markdown":
            return True, "The **hypopg** extension is already installed."
        else:
            return True, "The hypopg extension is already installed."

    detection = await platform() if platform is not None else await detect_platform(sql_driver)
    text = _Text(markdown=message_type == "markdown")
    allowlist = await _azure_allowlist(sql_driver) if detection.platform is Platform.AZURE else None

    if status.is_available:
        return False, text.paragraphs(_create_hypopg(text, detection, status.default_version, allowlist))

    pg_version = await get_postgres_version(sql_driver)
    return False, text.paragraphs(_hypopg_unavailable(text, detection, pg_version, allowlist))


class _Text:
    """Writes the same advice as markdown or as plain text."""

    def __init__(self, markdown: bool):
        self.markdown = markdown

    def code(self, text: str) -> str:
        return f"`{text}`" if self.markdown else text

    def name(self, text: str) -> str:
        return f"**{text}**" if self.markdown else text

    def paragraphs(self, parts: list[str]) -> str:
        return ("\n\n" if self.markdown else "\n").join(part for part in parts if part)

    def create(self) -> str:
        """The two ways to create HypoPG: the command, and the library's call."""
        command = self.code(f'pg-debug execute "{_CREATE_HYPOPG}"')
        library = self.code(f'PgDebug.execute_sql("{_CREATE_HYPOPG}")')
        return f"{command}, or {library} from Python"

    def without(self) -> str:
        """What still works without HypoPG."""
        return (
            f"{self.code('explain')}, {self.code('diagnose')} and {self.code('index-use')} still work without it, "
            "and the plan they read is the evidence there is."
        )


def _identified(detection: PlatformDetection) -> str:
    """The signal the advice rests on, so that a wrong detection can be seen to be one."""
    if not detection.known:
        return ""
    return f"pg-debug took this server for {detection.platform.label} because {detection.signal}."


def _create_hypopg(text: _Text, detection: PlatformDetection, version: str | None, allowlist: list[str] | None) -> list[str]:
    """Advice for a server that lists HypoPG as available: create it, after asking."""
    platform = detection.platform
    listed = f"this server lists it as available (version {version})" if version else "this server lists it as available"
    if platform in _LACK_HYPOPG:
        # What the server lists outranks what the platform documents.
        where = f"{platform.label} is documented not to offer it, but {listed}."
    elif platform in _OFFER_HYPOPG:
        where = f"{platform.label} offers it, and {listed}."
    else:
        where = f"{listed[0].upper()}{listed[1:]}."

    parts = [f"The {text.name('hypopg')} extension is required to simulate indexes, but it has not been created in this database. {where}"]
    if platform is Platform.AZURE:
        parts.append(_azure_allowlisting(text, allowlist))
        parts.append(_azure_create(text, "Then create it"))
    else:
        parts.append(f"Create it with {text.create()}.")
    parts.append(_ask_first(text))
    parts.append(_identified(detection))
    return parts


def _ask_first(text: _Text) -> str:
    """Why creating HypoPG is the database owner's call, and what it does."""
    return (
        "It is a write, so restricted mode refuses it, and it changes the database, so ask whoever owns the database before "
        "running it. HypoPG only simulates indexes, in the memory of the session that asks for them, and creates none; "
        f"{text.code('DROP EXTENSION hypopg')} removes it again."
    )


def _azure_create(text: _Text, lead: str) -> str:
    """How to create HypoPG on Azure, and where not to."""
    return f"{lead} with {text.create()}, and not in {text.code('pg_catalog')}: Azure's autonomous tuning drops a copy installed there."


def _azure_allowlisting(text: _Text, allowlist: list[str] | None) -> str:
    """What azure.extensions allows, and what to change in it."""
    setting = text.code("azure.extensions")
    if allowlist is None:
        return (
            f"On Azure an extension has to be allowlisted before it can be created: add hypopg to the {setting} server "
            f"parameter, in the portal or the CLI. {text.code('SHOW azure.extensions')} lists what it allows now."
        )
    if "hypopg" in allowlist:
        return f"On Azure an extension has to be allowlisted before it can be created, and {setting} already allows hypopg."
    allowed = ", ".join(allowlist) if allowlist else "nothing"
    return (
        f"On Azure an extension has to be allowlisted before it can be created, and {setting} allows {allowed}: "
        "add hypopg to that server parameter first, in the portal or the CLI."
    )


def _hypopg_unavailable(text: _Text, detection: PlatformDetection, pg_version: int, allowlist: list[str] | None) -> list[str]:
    """Advice for a server that does not list HypoPG at all."""
    platform = detection.platform
    unlisted = f"this server does not list it in {text.code('pg_available_extensions')}"
    if platform in _LACK_HYPOPG:
        return [
            f"{platform.label} does not offer the {text.name('hypopg')} extension, and {unlisted}, so pg-debug cannot "
            f"simulate an index here. {text.without()}",
            _identified(detection),
        ]
    if platform in _OFFER_HYPOPG:
        version = f"; this server runs PostgreSQL {pg_version}" if pg_version > 0 else ""
        parts = [
            f"The {text.name('hypopg')} extension is required to simulate indexes, and {unlisted}, though {platform.label} "
            f"documents it as one it offers. {platform.label}'s list of supported extensions says which PostgreSQL versions "
            f"have it{version}.",
        ]
        if platform is Platform.AZURE:
            # Whether Azure lists an extension before it is allowlisted is not
            # documented, so the allowlist may be why it is missing.
            parts += [_azure_allowlisting(text, allowlist), _azure_create(text, "Once this server lists it, create it"), _ask_first(text)]
        parts += [f"Until it can be created, pg-debug cannot simulate an index here. {text.without()}", _identified(detection)]
        return parts

    major_version_str = f"{pg_version}" if pg_version > 0 else "XX"
    steps = [
        "To install HypoPG:",
        f"1. For Debian/Ubuntu: {text.code(f'sudo apt-get install postgresql-{major_version_str}-hypopg')}",
        f"2. For RHEL/CentOS: {text.code(f'sudo yum install postgresql{major_version_str}-hypopg')}",
        f"3. For MacOS with Homebrew: {text.code('brew install hypopg')}",
        f"4. For other systems, build from source: {text.code('git clone https://github.com/HypoPG/hypopg')}",
    ]
    return [
        f"The {text.name('hypopg')} extension is not available on this PostgreSQL server.",
        "\n".join(steps),
        f"After installing the package, create the extension with {text.create()}.",
        f"That is for a server you administer. pg-debug recognised no managed platform here ({detection.signal}), and a "
        "managed platform does not let you install packages: if this is one, its list of supported extensions says whether "
        "it offers HypoPG.",
    ]


async def _azure_allowlist(sql_driver: SqlDriver) -> list[str] | None:
    """The extensions ``azure.extensions`` allows, or None if it could not be read."""
    try:
        rows = await sql_driver.execute_query("SELECT current_setting('azure.extensions', true) AS allowed")
    except QueryError as e:
        logger.info("Could not read azure.extensions: %s", e)
        return None
    value = rows[0].cells.get("allowed") if rows else None
    if not isinstance(value, str):
        return None
    return [name.strip().lower() for name in value.split(",") if name.strip()]
