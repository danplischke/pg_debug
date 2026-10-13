"""Extension and server version checks."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from pg_debug.db import get_postgres_version
from pg_debug.errors import QueryError


@pytest.mark.asyncio
async def test_a_server_version_that_cannot_be_read_is_a_query_error():
    driver = MagicMock()
    driver.execute_query = AsyncMock(side_effect=RuntimeError("connection reset"))

    with pytest.raises(QueryError, match=r"^Error determining PostgreSQL version: connection reset$"):
        await get_postgres_version(driver)
