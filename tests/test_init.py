"""Integration tests (config flow, setup, notify, action, events)."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.signal_wsrpc.const import (
    CONF_ACCOUNT,
    CONF_DEVICE_KEY,
    CONF_INVITE_CODE,
    CONF_RECIPIENT,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    EVENT_MESSAGE_RECEIVED,
    SUBENTRY_RECIPIENT,
)

from .fake_server import FakeServerState, push_text_message

ACCOUNT = "+380500000000"


async def test_user_flow_without_auth(hass: HomeAssistant, fake_server: str, mock_setup_entry) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake_server, CONF_ACCOUNT: ACCOUNT, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Signal {ACCOUNT}"
    assert CONF_TOKEN not in result["data"]


async def test_user_flow_with_invite(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, mock_setup_entry
) -> None:
    server_state.auth_enabled = True
    server_state.invites.add("INV-1")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake_server, CONF_ACCOUNT: ACCOUNT, CONF_INVITE_CODE: "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_INVITE_CODE: "invalid_invite"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake_server, CONF_ACCOUNT: ACCOUNT, CONF_INVITE_CODE: "INV-1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN].startswith("ptzh_")
    assert "PRIVATE KEY" in result["data"][CONF_DEVICE_KEY]
    assert CONF_INVITE_CODE not in result["data"]


async def test_cannot_connect(hass: HomeAssistant, socket_enabled) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ws://127.0.0.1:1/", CONF_ACCOUNT: ACCOUNT}
    )
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.fixture
def entry(fake_server: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Signal {ACCOUNT}",
        unique_id=ACCOUNT,
        data={CONF_URL: fake_server, CONF_ACCOUNT: ACCOUNT, CONF_VERIFY_SSL: True},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_RECIPIENT: "+380501111111"},
                subentry_type=SUBENTRY_RECIPIENT,
                title="Artem",
                unique_id="+380501111111",
            )
        ],
    )


async def test_setup_notify_action_event(
    hass: HomeAssistant, entry: MockConfigEntry, server_state: FakeServerState
) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("binary_sensor.signal_380500000000_connection").state == "on"
    notify_state = hass.states.get("notify.artem")
    assert notify_state is not None

    await hass.services.async_call(
        "notify", "send_message", {"entity_id": "notify.artem", "title": "HA", "message": "Двері"}, blocking=True
    )
    response = await hass.services.async_call(
        DOMAIN,
        "send_message",
        {"recipients": ["+380502222222"], "message": "test"},
        blocking=True,
        return_response=True,
    )
    assert response == {"timestamp": 1700000000000}
    assert [m["message"] for m in server_state.sent] == ["HA\nДвері", "test"]
    assert server_state.sent[1]["recipients"] == ["+380502222222"]

    events = async_capture_events(hass, EVENT_MESSAGE_RECEIVED)
    await push_text_message(server_state, "увімкни світло")
    await hass.async_block_till_done()
    for _ in range(20):
        if events:
            break
        await hass.async_block_till_done(wait_background_tasks=True)
    assert events[0].data["message"] == "увімкни світло"
    assert events[0].data["source"] == "+380501111111"
    assert events[0].data["source_name"] == "Tester"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_bad_token_starts_reauth(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState
) -> None:
    from custom_components.signal_wsrpc.client import generate_device_key

    server_state.auth_enabled = True
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        data={
            CONF_URL: fake_server,
            CONF_ACCOUNT: ACCOUNT,
            CONF_TOKEN: "ptzh_revoked",
            CONF_DEVICE_KEY: generate_device_key(),
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_auth_required_error(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState
) -> None:
    server_state.auth_enabled = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake_server, CONF_ACCOUNT: ACCOUNT}
    )
    assert result["errors"] == {"base": "auth_required"}
    assert "401" in result["description_placeholders"]["error"]


async def test_cannot_connect_has_reason(hass: HomeAssistant, socket_enabled) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://127.0.0.1:1/", CONF_ACCOUNT: ACCOUNT}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert "ws://" in result["description_placeholders"]["error"]
