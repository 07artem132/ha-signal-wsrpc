"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from aiohttp.test_utils import TestServer

from .fake_server import FakeServerState, close_all, make_app


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


@pytest.fixture
def server_state() -> FakeServerState:
    return FakeServerState()


@pytest.fixture
async def fake_server(server_state: FakeServerState, socket_enabled) -> AsyncGenerator[str]:
    server = TestServer(make_app(server_state), host="127.0.0.1")
    await server.start_server()
    yield f"ws://127.0.0.1:{server.port}/"
    await close_all(server_state)
    await server.close()


@pytest.fixture
def mock_setup_entry():
    """Skip real setup after a flow creates the entry."""
    from unittest.mock import patch

    with patch("custom_components.signal_wsrpc.async_setup_entry", return_value=True) as mock:
        yield mock
