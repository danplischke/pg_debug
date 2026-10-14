"""Recognising a managed platform, and saying what the recognition rests on.

None of the signals has been tried on a live platform, so these tests pin
what each one is taken to mean and in which order they are trusted, and that
a server nothing matches stays unknown rather than being called self-hosted.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import SafeSqlDriver
from pg_debug.db import SqlDriver
from pg_debug.errors import QueryError
from pg_debug.platform import PLATFORM_ROLES
from pg_debug.platform import PLATFORM_SQL
from pg_debug.platform import Platform
from pg_debug.platform import detect_from_url
from pg_debug.platform import detect_platform
from pg_debug.platform import hosts_in

NOTHING = {
    "neon_timeline_id": False,
    "supautils_privileged_role": False,
    "aurora_version": False,
    "rds_extensions": False,
    "azure_extensions": False,
    "platform_roles": [],
}


def server(**signals) -> MagicMock:
    """A driver whose server answers the probe with these signals and nothing else."""
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(return_value=[SqlDriver.RowResult(cells={**NOTHING, **signals})])
    return driver


# -- The host -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("postgresql://u:p@ep-cool-darkness-123456.us-east-2.aws.neon.tech/neondb", Platform.NEON),
        ("postgresql://u:p@ep-cool-darkness-123456-pooler.us-east-2.aws.neon.tech/neondb", Platform.NEON),
        ("postgresql://postgres:p@db.abcdefghijklmnop.supabase.co:5432/postgres", Platform.SUPABASE),
        ("postgresql://postgres.abc:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres", Platform.SUPABASE),
        ("postgresql://u:p@aws-us-east-1-1.horizon.psdb.cloud/postgres", Platform.PLANETSCALE),
        ("postgresql://u:p@p.abcdefghijklmnop.db.postgresbridge.com:5432/postgres", Platform.CRUNCHY_BRIDGE),
        ("postgres://u:p@db.prisma.io:5432/postgres", Platform.PRISMA_POSTGRES),
        ("postgresql://tsdbadmin:p@abc123.def456.tsdb.cloud.timescale.com:33001/tsdb", Platform.TIGER_CLOUD),
        ("postgresql://admin@abcdefghijklmnopqrstuvwxyz.dsql.us-east-1.on.aws/postgres", Platform.AURORA_DSQL),
    ],
)
def test_a_host_under_a_platforms_domain_names_the_platform(url, platform):
    detection = detect_from_url(url)

    assert detection is not None
    assert detection.platform is platform
    assert hosts_in(url)[0] in detection.signal, "the signal names the host it read"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/db",
        "postgresql://db.internal.example.com/db",
        # A name that merely contains a platform's domain is not under it.
        "postgresql://neon.tech.example.com/db",
        "postgresql://myneon.tech/db",
        # RDS, Aurora, Cloud SQL, AlloyDB and Azure hosts are not recognised:
        # their names are not documented as under a domain of their own.
        "postgresql://mydb.abc123.us-east-1.rds.amazonaws.com/db",
        # on.aws without .dsql. is some other AWS endpoint.
        "postgresql://something.us-east-1.on.aws/db",
        None,
        "",
    ],
)
def test_other_hosts_name_no_platform(url):
    assert detect_from_url(url) is None


def test_a_conninfo_string_names_its_host_too():
    detection = detect_from_url("host=ep-x.us-east-2.aws.neon.tech dbname=neondb user=u")

    assert detection is not None
    assert detection.platform is Platform.NEON


def test_every_host_of_a_list_is_read():
    assert hosts_in("postgresql://u@primary.example.com,ep-x.aws.neon.tech/db") == ["primary.example.com", "ep-x.aws.neon.tech"]


def test_a_host_is_read_case_insensitively_and_without_a_trailing_dot():
    detection = detect_from_url("postgresql://u@EP-X.AWS.NEON.TECH./db")

    assert detection is not None
    assert detection.platform is Platform.NEON


def test_pghost_is_read_when_the_url_names_no_host(monkeypatch):
    """libpq connects there, so that is the host the server is on."""
    monkeypatch.setenv("PGHOST", "ep-x.aws.neon.tech")

    assert hosts_in("postgresql:///neondb") == ["ep-x.aws.neon.tech"]


def test_a_string_psycopg_cannot_parse_names_no_host():
    assert hosts_in("not a conninfo string") == []


# -- The server ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("signal", "platform", "evidence"),
    [
        ("neon_timeline_id", Platform.NEON, "neon.timeline_id"),
        ("supautils_privileged_role", Platform.SUPABASE, "supautils.privileged_role"),
        ("aurora_version", Platform.AURORA, "aurora_version()"),
        ("rds_extensions", Platform.RDS, "rds.extensions"),
        ("azure_extensions", Platform.AZURE, "azure.extensions"),
    ],
)
@pytest.mark.asyncio
async def test_a_platforms_own_setting_or_function_names_it(signal, platform, evidence):
    detection = await detect_platform(server(**{signal: True}))

    assert detection.platform is platform
    assert evidence in detection.signal


@pytest.mark.asyncio
async def test_aurora_is_told_from_rds_by_its_function():
    """Aurora carries the RDS settings too."""
    detection = await detect_platform(server(aurora_version=True, rds_extensions=True, platform_roles=["rds_superuser"]))

    assert detection.platform is Platform.AURORA


@pytest.mark.parametrize(
    ("role", "platform"),
    [
        ("neon_superuser", Platform.NEON),
        ("rds_superuser", Platform.RDS),
        ("pscale_superuser", Platform.PLANETSCALE),
        ("cloudsqlsuperuser", Platform.CLOUD_SQL),
        ("alloydbsuperuser", Platform.ALLOYDB),
        ("azure_pg_admin", Platform.AZURE),
        ("tsdbadmin", Platform.TIGER_CLOUD),
    ],
)
@pytest.mark.asyncio
async def test_a_platforms_role_names_it_when_nothing_stronger_does(role, platform):
    detection = await detect_platform(server(platform_roles=[role]))

    assert detection.platform is platform
    assert detection.signal == f"the role {role} exists"


@pytest.mark.asyncio
async def test_a_setting_outranks_the_host_and_the_host_outranks_a_role():
    neon_host = "postgresql://u@ep-x.aws.neon.tech/db"

    assert (await detect_platform(server(supautils_privileged_role=True), neon_host)).platform is Platform.SUPABASE
    assert (await detect_platform(server(platform_roles=["tsdbadmin"]), neon_host)).platform is Platform.NEON


@pytest.mark.asyncio
async def test_roles_of_two_platforms_are_no_answer():
    """Anyone can create a role; two platforms' worth of them names neither."""
    detection = await detect_platform(server(platform_roles=["cloudsqlsuperuser", "rds_superuser"]))

    assert detection.platform is Platform.UNKNOWN
    assert "cloudsqlsuperuser" in detection.signal
    assert "rds_superuser" in detection.signal


@pytest.mark.asyncio
async def test_a_server_nothing_matches_is_unknown_not_self_hosted():
    detection = await detect_platform(server(), "postgresql://localhost/db")

    assert detection.platform is Platform.UNKNOWN
    assert not detection.known
    assert "none of the signals" in detection.signal
    assert "self-hosted" not in detection.signal


@pytest.mark.asyncio
async def test_a_refused_probe_leaves_the_platform_to_the_host():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(side_effect=QueryError('function "to_regproc" does not exist'))

    unknown = await detect_platform(driver, "postgresql://localhost/db")
    by_host = await detect_platform(driver, "postgresql://u@ep-x.aws.neon.tech/db")

    assert unknown.platform is Platform.UNKNOWN
    assert "to_regproc" in unknown.signal, "the reason the server gave is kept"
    assert by_host.platform is Platform.NEON


@pytest.mark.asyncio
async def test_aurora_dsql_is_decided_from_the_host_without_running_anything():
    """DSQL is not PostgreSQL underneath, and the probe is not known to work there."""
    driver = server()

    detection = await detect_platform(driver, "postgresql://admin@abc.dsql.us-east-1.on.aws/postgres")

    assert detection.platform is Platform.AURORA_DSQL
    driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_boolean_is_no_answer():
    detection = await detect_platform(server(neon_timeline_id="on", platform_roles="neon_superuser"))

    assert detection.platform is Platform.UNKNOWN


def test_the_probe_is_one_restricted_mode_allows():
    SafeSqlDriver(MagicMock(spec=SqlDriver))._validate(PLATFORM_SQL)


def test_the_probe_asks_for_every_role_the_detector_knows():
    for role in PLATFORM_ROLES:
        assert f"'{role}'" in PLATFORM_SQL


def test_only_aurora_dsql_refuses_a_statement_timeout():
    assert [platform for platform in Platform if platform.refuses_statement_timeout] == [Platform.AURORA_DSQL]


def test_every_platform_has_a_name_to_print():
    assert all(platform.label for platform in Platform)
