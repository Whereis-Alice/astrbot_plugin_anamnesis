"""A failed consolidation must be visible as a Page API error."""

from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_anamnesis.core.page_api_modules.consolidation_handler import (
    ConsolidationHandler,
)
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils


@pytest.mark.asyncio
async def test_manager_error_is_not_reported_as_success():
    manager = Mock()
    manager.run_consolidation = AsyncMock(
        return_value={"error": "candidate query failed"}
    )

    response = await ConsolidationHandler(PageApiUtils()).run(manager)

    assert response["status"] == "error"
    assert "candidate query failed" in response["message"]
