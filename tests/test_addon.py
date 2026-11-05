"""Add-on discovery (HAOS): Supervisor → hassio step → QR link → entry."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_HASSIO
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.signal_wsrpc.const import CONF_ACCOUNT, CONF_MODE, CONF_URL, DOMAIN, MODE_ADDON

from .fake_server import FakeServerState


def _info(url: str) -> HassioServiceInfo:
    port = int(url.rstrip("/").rsplit(":", 1)[1])
    return HassioServiceInfo(
        config={"host": "127.0.0.1", "port": port},
        name="Signal WsRpcServer",
        slug="signal_wsrpc_server",
        uuid="addon-uuid",
    )


def _url(fake_server: str) -> str:
    return fake_server.rstrip("/")


async def _run_progress(hass: HomeAssistant, result):
    flow_id = result["flow_id"]
    for _ in range(6):
        if result["type"] is not FlowResultType.SHOW_PROGRESS:
            break
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    return result


async def test_addon_discovery_links_account(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, mock_setup_entry
) -> None:
    assert await async_setup_component(hass, "http", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_info(fake_server)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "link"
    result = await _run_progress(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"] == {CONF_MODE: MODE_ADDON, CONF_URL: _url(fake_server), CONF_ACCOUNT: "+380500000000"}
    assert result["result"].unique_id == "+380500000000"
    assert server_state.link_requests == [{"sessionId": "link-1", "deviceName": "Home Assistant"}]


async def test_addon_discovery_reuses_linked_account(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, mock_setup_entry
) -> None:
    """Account already linked on the server (integration re-added) → no second linked device."""
    server_state.accounts = ["+380671112233"]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_info(fake_server)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_ACCOUNT] == "+380671112233"
    assert server_state.link_requests == []


async def test_addon_rediscovery_updates_port(hass: HomeAssistant, fake_server: str, mock_setup_entry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="+380500000000",
        data={CONF_MODE: MODE_ADDON, CONF_URL: "ws://127.0.0.1:1", CONF_ACCOUNT: "+380500000000"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_info(fake_server)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_URL] == _url(fake_server)


async def test_addon_entry_cannot_be_reconfigured(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="+380500000000",
        data={CONF_MODE: MODE_ADDON, CONF_URL: "ws://127.0.0.1:9876", CONF_ACCOUNT: "+380500000000"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "managed_no_reconfigure"
