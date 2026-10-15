"""How the client learns the platform, and what it changes on the way.

It detects the platform once, hands it to the configuration review and to the
HypoPG advice, and keeps the statement timeout off a server whose host says
Aurora DSQL, which refuses ``statement_timeout`` at any scope. The last rests
on DSQL's documentation and is stubbed here: there is no DSQL to run against.
"""

import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from pg_debug import AccessMode
from pg_debug import PgDebug
from pg_debug.db import DbConnPool
from pg_debug.db import SqlDriver
from pg_debug.db.extensions import ExtensionStatus
from pg_debug.errors import ExtensionRequiredError
from pg_debug.platform import Basis
from pg_debug.platform import Platform
from pg_debug.platform import PlatformDetection

DSQL_URL = "postgresql://admin@abcdefghijklmnopqrstuvwxyz.dsql.us-east-1.on.aws/postgres"
NEON = PlatformDetection(Platform.NEON, "the server has the setting neon.timeline_id", Basis.SERVER)
CLOUD_SQL = PlatformDetection(Platform.CLOUD_SQL, "the role cloudsqlsuperuser exists", Basis.ROLE)


@pytest.fixture
def pg():
    pool = MagicMock(spec=DbConnPool)
    pool.is_valid = True
    pool.close = AsyncMock()
    client = PgDebug(pool=pool)
    with patch.object(PgDebug, "sql_driver", AsyncMock(return_value=MagicMock(spec=SqlDriver))):
        yield client


def missing_hypopg(available: bool = False):
    status = ExtensionStatus(is_installed=False, is_available=available, name="hypopg", message="", default_version=None)
    return patch("pg_debug.db.extensions.check_extension", AsyncMock(return_value=status))


# -- Detected once, used where it matters --------------------------------------


@pytest.mark.asyncio
async def test_the_platform_is_detected_once_per_client(pg):
    with (
        patch("pg_debug.client.detect_platform", AsyncMock(return_value=NEON)) as detect,
        patch("pg_debug.client.ConfigurationReviewer") as reviewer_cls,
    ):
        reviewer_cls.return_value.review = AsyncMock(return_value=[])
        await pg.review_configuration()
        await pg.review_configuration()

    detect.assert_awaited_once()
    assert reviewer_cls.call_args.kwargs["platform"] is NEON


@pytest.mark.asyncio
async def test_tuning_without_hypopg_says_what_this_platform_offers(pg):
    with (
        missing_hypopg(),
        patch("pg_debug.db.extensions.get_postgres_version", AsyncMock(return_value=16)),
        patch("pg_debug.client.detect_platform", AsyncMock(return_value=CLOUD_SQL)),
        patch("pg_debug.client.TextPresentation") as presentation_cls,
    ):
        result = await pg.analyze_query_indexes(["SELECT 1"])
        with pytest.raises(ExtensionRequiredError) as excinfo:
            await pg.analyze_query_indexes(["SELECT 1"], method="llm")

    # The dta method falls back to candidates from statistics, and says why.
    assert result["costed"] is False
    assert "Cloud SQL does not offer" in result["hypopg"]
    assert "apt-get" not in result["hypopg"]
    # The llm method cannot do without the planner, and refuses as before.
    assert "Cloud SQL does not offer" in str(excinfo.value)
    assert "apt-get" not in str(excinfo.value)
    presentation_cls.return_value.analyze_queries.assert_not_called()


@pytest.mark.asyncio
async def test_a_hypothetical_explain_asks_for_the_platform_only_when_hypopg_is_missing(pg):
    installed = ExtensionStatus(is_installed=True, is_available=True, name="hypopg", message="", default_version=None)
    with (
        patch("pg_debug.db.extensions.check_extension", AsyncMock(return_value=installed)),
        patch("pg_debug.client.detect_platform", AsyncMock(return_value=NEON)) as detect,
        patch("pg_debug.client.ExplainPlanTool") as tool_cls,
    ):
        tool_cls.return_value.explain_with_hypothetical_indexes = AsyncMock()
        await pg.explain_query("SELECT 1", hypothetical_indexes=[{"table": "t", "columns": ["c"]}])

    detect.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_hypothetical_explain_without_hypopg_names_the_real_command(pg):
    with missing_hypopg(available=True), patch("pg_debug.client.detect_platform", AsyncMock(return_value=NEON)):
        with pytest.raises(ExtensionRequiredError) as excinfo:
            await pg.explain_query("SELECT 1", hypothetical_indexes=[{"table": "t", "columns": ["c"]}])

    assert 'pg-debug execute "CREATE EXTENSION hypopg"' in str(excinfo.value)
    assert "execute_query" not in str(excinfo.value)


# -- Aurora DSQL: no statement_timeout on the server ---------------------------------


async def connect(pg: PgDebug) -> None:
    with patch.object(DbConnPool, "pool_connect", AsyncMock()):
        await pg.connect()


@pytest.mark.asyncio
async def test_a_dsql_host_keeps_restricted_modes_timeout_on_this_side(caplog):
    pg = PgDebug(DSQL_URL, access_mode=AccessMode.RESTRICTED)

    with caplog.at_level(logging.WARNING, logger="pg_debug.client"):
        await connect(pg)

    assert pg.pool.server_side_timeout is False
    assert pg.pool.statement_timeout == 30, "the bound is kept, on this side"
    assert SqlDriver(conn=pg.pool)._transaction_setup(in_context=False) is None, "nothing for DSQL to refuse"
    assert "Aurora DSQL refuses statement_timeout" in caplog.text
    assert "30-second timeout is not set on the server" in caplog.text


@pytest.mark.asyncio
async def test_a_dsql_host_keeps_an_explicit_timeout_on_this_side_in_unrestricted_mode(caplog):
    pg = PgDebug(DSQL_URL, timeout=5)

    with caplog.at_level(logging.WARNING, logger="pg_debug.client"):
        await connect(pg)

    assert pg.pool.server_side_timeout is False
    assert pg.pool.statement_timeout == 5
    assert "5-second timeout" in caplog.text


@pytest.mark.asyncio
async def test_a_dsql_host_is_never_sent_a_timeout_even_a_zero_one(caplog):
    """--timeout 0 would set statement_timeout to 0s, which DSQL refuses as well."""
    pg = PgDebug(DSQL_URL, timeout=0)

    with caplog.at_level(logging.WARNING, logger="pg_debug.client"):
        await connect(pg)

    assert SqlDriver(conn=pg.pool)._transaction_setup(in_context=False) is None
    assert caplog.text == "", "there is no bound to warn about"


@pytest.mark.parametrize(
    "url",
    [
        # PrivateLink with private DNS: the service's identifier in dsql's place.
        "postgresql://admin@abcdefghij.dsql-fnh4.us-east-1.on.aws/postgres",
        # PrivateLink without it: the endpoint's own name, and the cluster in the options.
        "postgresql://admin@vpce-04037adb76c111221-d849uc2p.dsql-fnh4.us-east-1.vpce.amazonaws.com/postgres"
        "?options=-c%20amzn-cluster-id%3Dabcdefghij",
        # Or the endpoint's address.
        "postgresql://admin@10.0.1.5/postgres?options=-c%20amzn-cluster-id%3Dabcdefghij",
    ],
)
@pytest.mark.asyncio
async def test_a_dsql_cluster_reached_through_privatelink_keeps_the_timeout_on_this_side(url, caplog):
    pg = PgDebug(url, access_mode=AccessMode.RESTRICTED)

    with caplog.at_level(logging.WARNING, logger="pg_debug.client"):
        await connect(pg)

    assert pg.pool.server_side_timeout is False
    assert SqlDriver(conn=pg.pool)._transaction_setup(in_context=False) is None
    assert "Aurora DSQL refuses statement_timeout" in caplog.text


@pytest.mark.asyncio
async def test_a_dsql_cluster_named_in_pgoptions_keeps_the_timeout_on_this_side(monkeypatch):
    """AWS's own example for PrivateLink without private DNS sets the cluster in PGOPTIONS."""
    monkeypatch.setenv("PGOPTIONS", "-c amzn-cluster-id=abcdefghij")
    pg = PgDebug("postgresql://admin@vpce-04037adb76c111221-d849uc2p.example.internal/postgres", timeout=5)

    await connect(pg)

    assert pg.pool.server_side_timeout is False


@pytest.mark.asyncio
async def test_any_other_host_keeps_the_timeout_on_the_server():
    pg = PgDebug("postgresql://u@ep-x.aws.neon.tech/neondb", access_mode=AccessMode.RESTRICTED)

    await connect(pg)

    assert pg.pool.server_side_timeout is True


@pytest.mark.asyncio
async def test_a_pool_handed_in_is_its_owners_to_configure():
    supplied = DbConnPool(DSQL_URL, statement_timeout=30)
    pg = PgDebug(DSQL_URL, access_mode=AccessMode.RESTRICTED, pool=supplied)

    await connect(pg)

    assert supplied.server_side_timeout is True
