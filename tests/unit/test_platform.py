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
from pg_debug.platform import Basis
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
        # Through PrivateLink with private DNS, the service's identifier takes dsql's place.
        ("postgresql://admin@abcdefghijklmnopqrstuvwxyz.dsql-fnh4.us-east-1.on.aws/postgres", Platform.AURORA_DSQL),
        # Through PrivateLink without it, the endpoint's own name.
        ("postgresql://admin@vpce-04037adb76c111221-d849uc2p.dsql-fnh4.us-east-1.vpce.amazonaws.com/postgres", Platform.AURORA_DSQL),
        ("postgresql://role_name:p@ep-abc-123.databricks.com/databricks_postgres?sslmode=require", Platform.LAKEBASE),
        ("postgresql://role_name:p@instance-123abcdef456.database.cloud.databricks.com/databricks_postgres", Platform.LAKEBASE),
    ],
)
def test_a_host_under_a_platforms_domain_names_the_platform(url, platform):
    detection = detect_from_url(url)

    assert detection is not None
    assert detection.platform is platform
    assert detection.basis is Basis.URL
    assert hosts_in(url)[0] in detection.signal, "the signal names the host it read"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://admin@10.0.1.5/postgres?options=-c%20amzn-cluster-id%3Dabcdefghij",
        "host=10.0.1.5 dbname=postgres user=admin options='-c amzn-cluster-id=abcdefghij'",
        "host=10.0.1.5 dbname=postgres options='-camzn_cluster_id=abcdefghij'",
        "postgresql://admin@vpce-0123.example.internal/postgres?options=--amzn-cluster-id%3Dabcdefghij",
    ],
)
def test_the_amzn_cluster_id_option_names_aurora_dsql_whatever_the_host(url):
    """Without private DNS the host can be the endpoint's address; DSQL takes the cluster from the option."""
    detection = detect_from_url(url)

    assert detection is not None
    assert detection.platform is Platform.AURORA_DSQL
    assert "amzn-cluster-id" in detection.signal


def test_pgoptions_is_read_when_the_url_sets_no_options(monkeypatch):
    """AWS's own example connects this way: PGOPTIONS, and the endpoint's name as the host."""
    monkeypatch.setenv("PGOPTIONS", "-c amzn-cluster-id=abcdefghij")

    detection = detect_from_url("postgresql://admin@10.0.1.5/postgres")

    assert detection is not None
    assert detection.platform is Platform.AURORA_DSQL


def test_pgoptions_is_not_read_when_the_url_sets_options_of_its_own(monkeypatch):
    """libpq sends the URL's options then, not PGOPTIONS."""
    monkeypatch.setenv("PGOPTIONS", "-c amzn-cluster-id=abcdefghij")

    assert detect_from_url("postgresql://admin@10.0.1.5/postgres?options=-c%20search_path%3Dapp") is None


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
        # on.aws or vpce.amazonaws.com without a dsql label is some other AWS endpoint.
        "postgresql://something.us-east-1.on.aws/db",
        "postgresql://abc.lambda-url.us-east-1.on.aws/db",
        "postgresql://vpce-0123-abcd.s3.us-east-1.vpce.amazonaws.com/db",
        "postgresql://abc.dsqlx.us-east-1.on.aws/db",
        # A dsql label with no cluster or endpoint name in front of it names no cluster.
        "postgresql://dsql.us-east-1.on.aws/db",
        # A dsql label elsewhere is not under DSQL's domains.
        "postgresql://abc.dsql.us-east-1.example.com/db",
        # Databricks hosts that are not Lakebase endpoints, and a name that merely contains one.
        "postgresql://dbc-1234abcd-5678.cloud.databricks.com/db",
        "postgresql://ep-abc-123.databricks.com.example.com/db",
        # An option that merely mentions amzn-cluster-id sets something else.
        "postgresql://u@10.0.1.5/db?options=-c%20application_name%3Damzn-cluster-id",
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


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://admin@abc.dsql.us-east-1.on.aws/postgres",
        "postgresql://admin@abc.dsql-fnh4.us-east-1.on.aws/postgres",
        "postgresql://admin@vpce-0123-abcd.dsql-fnh4.us-east-1.vpce.amazonaws.com/postgres?options=-c%20amzn-cluster-id%3Dabc",
        "postgresql://admin@10.0.1.5/postgres?options=-c%20amzn-cluster-id%3Dabc",
    ],
)
@pytest.mark.asyncio
async def test_aurora_dsql_is_decided_from_the_url_without_running_anything(url):
    """DSQL is not PostgreSQL underneath, and the probe is not known to work there."""
    driver = server()

    detection = await detect_platform(driver, url)

    assert detection.platform is Platform.AURORA_DSQL
    driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lakebase_host_outranks_the_neon_settings_its_server_may_carry():
    """Lakebase is built on Neon: asked first, the server would be taken for Neon."""
    driver = server(neon_timeline_id=True, platform_roles=["neon_superuser"])

    detection = await detect_platform(driver, "postgresql://u@ep-abc-123.databricks.com/databricks_postgres")

    assert detection.platform is Platform.LAKEBASE
    assert "ep-abc-123.databricks.com" in detection.signal
    driver.execute_query.assert_not_awaited()


# -- What a detection rests on ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_detection_says_what_kind_of_signal_it_rests_on():
    by_setting = await detect_platform(server(neon_timeline_id=True))
    by_host = await detect_platform(server(), "postgresql://u@ep-x.aws.neon.tech/db")
    by_role = await detect_platform(server(platform_roles=["neon_superuser"]), "postgresql://u@db.internal.example.com/app")
    by_nothing = await detect_platform(server(), "postgresql://u@db.internal.example.com/app")

    assert [d.basis for d in (by_setting, by_host, by_role, by_nothing)] == [Basis.SERVER, Basis.URL, Basis.ROLE, Basis.NONE]
    assert [d.firm for d in (by_setting, by_host, by_role, by_nothing)] == [True, True, False, False]


# -- Saying why nothing matched ----------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_url_the_reason_says_no_host_was_read():
    """The index advisor asks without one; the reason must not claim a host was checked."""
    detection = await detect_platform(server())

    assert detection.platform is Platform.UNKNOWN
    assert "pg-debug had no connection URL to read a host from" in detection.signal
    assert "not the host" not in detection.signal
    assert "names no platform" not in detection.signal


@pytest.mark.asyncio
async def test_with_a_url_the_reason_says_it_named_no_platform():
    detection = await detect_platform(server(), "postgresql://u@db.internal.example.com/app")

    assert "the connection URL names no platform pg-debug knows" in detection.signal
    assert "no connection URL" not in detection.signal


@pytest.mark.asyncio
async def test_a_refused_probe_without_a_url_says_no_host_was_read():
    driver = MagicMock(spec=SqlDriver)
    driver.execute_query = AsyncMock(side_effect=QueryError("permission denied"))

    detection = await detect_platform(driver)

    assert detection.platform is Platform.UNKNOWN
    assert "permission denied" in detection.signal
    assert "pg-debug had no connection URL to read a host from" in detection.signal


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
