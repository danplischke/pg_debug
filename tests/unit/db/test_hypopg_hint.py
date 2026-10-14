"""What pg-debug says when HypoPG is missing, on each kind of platform.

The advice used to name an ``execute_query`` tool that does not exist, and to
tell every server without the package to apt-get it, including platforms that
let nobody install packages and do not offer HypoPG at all.
"""

from typing import Literal
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug.db import SqlDriver
from pg_debug.db import check_hypopg_installation_status
from pg_debug.db import reset_postgres_version_cache
from pg_debug.db.extensions import ExtensionStatus
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection

CREATE = 'pg-debug execute "CREATE EXTENSION hypopg"'


def status(installed=False, available=False, version="1.4.1") -> ExtensionStatus:
    return ExtensionStatus(is_installed=installed, is_available=available, name="hypopg", message="", default_version=version if available else None)


def on(platform: Platform, signal="the test says so") -> AsyncMock:
    return AsyncMock(return_value=PlatformDetection(platform, signal))


def driver(**settings) -> MagicMock:
    """A server on PostgreSQL 16 that answers the setting reads it is asked for."""

    async def answer(query, *args, **kwargs):
        if "server_version" in query:
            return [SqlDriver.RowResult(cells={"server_version": "16.4"})]
        if "azure.extensions" in query:
            return [SqlDriver.RowResult(cells={"allowed": settings.get("azure_extensions")})]
        raise AssertionError(f"unexpected query: {query}")

    mock = MagicMock(spec=SqlDriver)
    mock.execute_query = AsyncMock(side_effect=answer)
    return mock


async def hint(
    extension: ExtensionStatus,
    platform: AsyncMock | None = None,
    message_type: Literal["plain", "markdown"] = "markdown",
    **settings,
) -> tuple[bool, str]:
    # The version is cached for the process; this server's must not leak into other tests.
    reset_postgres_version_cache()
    try:
        with patch("pg_debug.db.extensions.check_extension", AsyncMock(return_value=extension)):
            return await check_hypopg_installation_status(driver(**settings), message_type=message_type, platform=platform)
    finally:
        reset_postgres_version_cache()


# -- Installed ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_installed_hypopg_needs_no_advice_and_no_platform():
    platform = on(Platform.NEON)

    installed, message = await hint(status(installed=True), platform=platform)

    assert installed
    assert "already installed" in message
    platform.assert_not_awaited()


# -- Offered and listed: create it, after asking -----------------------------------


@pytest.mark.parametrize(
    "platform",
    [Platform.NEON, Platform.SUPABASE, Platform.RDS, Platform.AURORA, Platform.ALLOYDB, Platform.PLANETSCALE, Platform.CRUNCHY_BRIDGE],
)
@pytest.mark.asyncio
async def test_where_it_is_offered_the_advice_is_to_create_it_with_the_real_command(platform):
    installed, message = await hint(status(available=True), platform=on(platform, "the host is the platform's"))

    assert not installed
    assert "execute_query" not in message, "no such tool exists"
    assert CREATE in message
    assert 'PgDebug.execute_sql("CREATE EXTENSION hypopg")' in message
    assert "ask whoever owns the database" in message
    assert "restricted mode refuses it" in message
    assert f"{platform.label} offers it" in message
    assert "because the host is the platform's" in message, "the advice says what it took the platform from"
    assert "apt-get" not in message


@pytest.mark.asyncio
async def test_an_unrecognised_server_that_lists_it_is_told_to_create_it_too():
    _, message = await hint(status(available=True), platform=on(Platform.UNKNOWN))

    assert CREATE in message
    assert "execute_query" not in message
    assert "This server lists it as available (version 1.4.1)." in message
    assert "took this server for" not in message


@pytest.mark.asyncio
async def test_what_the_server_lists_outranks_what_the_platform_documents():
    _, message = await hint(status(available=True), platform=on(Platform.CLOUD_SQL))

    assert "Cloud SQL is documented not to offer it, but this server lists it as available" in message
    assert CREATE in message


# -- Azure: allowlist first -------------------------------------------------------


@pytest.mark.asyncio
async def test_on_azure_the_allowlist_comes_first_with_what_it_allows_now():
    _, message = await hint(status(available=True), platform=on(Platform.AZURE), azure_extensions="PG_STAT_STATEMENTS,pgcrypto")

    assert "`azure.extensions` allows pg_stat_statements, pgcrypto" in message
    assert "add hypopg to that server parameter first" in message
    assert "not in `pg_catalog`" in message
    assert message.index("azure.extensions") < message.index(CREATE), "allowlisting comes before creating"


@pytest.mark.asyncio
async def test_on_azure_an_allowlist_that_has_it_already_is_said_to():
    _, message = await hint(status(available=True), platform=on(Platform.AZURE), azure_extensions="hypopg, pg_stat_statements")

    assert "already allows hypopg" in message
    assert CREATE in message


@pytest.mark.asyncio
async def test_on_azure_an_allowlist_that_cannot_be_read_is_pointed_at():
    _, message = await hint(status(available=True), platform=on(Platform.AZURE), azure_extensions=None)

    assert "`SHOW azure.extensions` lists what it allows now" in message


# -- Not offered: say so, and what still works ---------------------------------------


@pytest.mark.parametrize("platform", [Platform.CLOUD_SQL, Platform.TIGER_CLOUD, Platform.PRISMA_POSTGRES, Platform.AURORA_DSQL])
@pytest.mark.asyncio
async def test_where_it_is_not_offered_the_advice_says_so_plainly(platform):
    installed, message = await hint(status(), platform=on(platform))

    assert not installed
    assert f"{platform.label} does not offer the **hypopg** extension" in message
    assert "`explain`, `diagnose` and `index-use` still work" in message
    for package_manager in ("apt-get", "yum", "brew"):
        assert package_manager not in message
    assert "CREATE EXTENSION" not in message


@pytest.mark.asyncio
async def test_where_it_is_offered_but_not_listed_the_figure_is_given_not_a_package():
    _, message = await hint(status(), platform=on(Platform.NEON))

    assert "this server does not list it in `pg_available_extensions`" in message
    assert "Neon documents it as one it offers" in message
    assert "PostgreSQL 16" in message
    assert "apt-get" not in message


# -- Unknown: the packages, for a server someone administers -------------------------


@pytest.mark.asyncio
async def test_an_unrecognised_server_without_it_keeps_the_package_instructions():
    _, message = await hint(status(), platform=on(Platform.UNKNOWN, "none of the signals pg-debug knows matched"))

    assert "sudo apt-get install postgresql-16-hypopg" in message
    assert CREATE in message
    assert "execute_query" not in message
    assert "a server you administer" in message
    assert "none of the signals pg-debug knows matched" in message, "it says why no platform was recognised"


# -- Formats and callers ------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_text_says_the_same_without_markup():
    _, markdown = await hint(status(), platform=on(Platform.CLOUD_SQL), message_type="markdown")
    _, plain = await hint(status(), platform=on(Platform.CLOUD_SQL), message_type="plain")

    assert "`" not in plain
    assert "**" not in plain
    assert plain.replace("\n", " ") == markdown.replace("`", "").replace("**", "").replace("\n\n", " ")


@pytest.mark.asyncio
async def test_without_a_platform_the_server_is_asked():
    """The index advisor calls it this way, and knows no URL."""
    with patch(
        "pg_debug.db.extensions.detect_platform", AsyncMock(return_value=PlatformDetection(Platform.TIGER_CLOUD, "the role tsdbadmin exists"))
    ) as detect:
        _, message = await hint(status())

    detect.assert_awaited_once()
    assert "Tiger Cloud does not offer" in message
