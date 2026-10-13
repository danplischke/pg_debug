"""Sample values substituted for the placeholders pg_stat_statements records."""

from unittest.mock import MagicMock

import pytest

from pg_debug.errors import InvalidArgumentError
from pg_debug.parsing import SqlBindParams


@pytest.mark.asyncio
async def test_a_query_whose_parameters_cannot_be_filled_is_an_argument_error(monkeypatch):
    bind_params = SqlBindParams(MagicMock())

    def broken(query):
        raise RuntimeError("unexpected shape")

    monkeypatch.setattr(bind_params, "extract_columns", broken)

    with pytest.raises(InvalidArgumentError, match=r"^Error replacing parameters: unexpected shape$"):
        await bind_params.replace_parameters("SELECT * FROM orders WHERE customer_id = $1")
