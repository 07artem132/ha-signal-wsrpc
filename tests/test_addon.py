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
    for _ in range(8):
        if result["type"] is FlowResultType.FORM and result["step_id"] == "link":
            result = await hass.config_entries.flow.async_configure(flow_id, {})  # «Я відсканував»
            continue
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
    assert result["type"] is FlowResultType.FORM
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


async def test_user_menu_offers_addon_on_haos(hass: HomeAssistant, socket_enabled) -> None:
    from unittest.mock import patch

    from homeassistant.config_entries import SOURCE_USER

    with patch("custom_components.signal_wsrpc.config_flow.is_hassio", return_value=True), patch(
        "custom_components.signal_wsrpc.config_flow.ADDON_DEFAULT_URL", "ws://127.0.0.1:1"
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.MENU
        assert result["menu_options"] == ["addon", "external"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "addon"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_required"


async def test_qr_served_before_integration_setup(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, hass_client_no_auth, mock_setup_entry
) -> None:
    """Discovery flow runs before async_setup: the signed QR URL must still serve the PNG (was 404)."""
    import asyncio

    server_state.link_gate = asyncio.Event()
    assert await async_setup_component(hass, "http", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_info(fake_server)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    qr_url = result["description_placeholders"]["qr_url"]
    assert "authSig=" in qr_url

    client = await hass_client_no_auth()
    resp = await client.get(qr_url)
    assert resp.status == 200
    assert (await resp.read()).startswith(b"\x89PNG")
    assert (await client.get(qr_url.split("?")[0])).status == 401  # без підпису — ні
    server_state.link_gate.set()
    await hass.async_block_till_done()


async def test_link_timeout_offers_new_qr(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, mock_setup_entry
) -> None:
    """QR not scanned in time → retry step (flow and discovery survive) → new QR → entry."""
    from unittest.mock import patch

    assert await async_setup_component(hass, "http", {})
    with patch("custom_components.signal_wsrpc.config_flow.LINK_TIMEOUT", 0.05):
        import asyncio

        server_state.link_gate = asyncio.Event()  # телефон «не сканує» до таймауту
        # Відпускаємо fake-обробник трохи після таймауту, щоб close-handshake не чекав 10 с.
        asyncio.get_running_loop().call_later(0.3, server_state.link_gate.set)
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_HASSIO}, data=_info(fake_server)
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _run_progress(hass, result)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "link_retry"

        await asyncio.sleep(0.4)  # друга спроба: телефон сканує одразу
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _run_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_MODE] == MODE_ADDON


async def test_user_addon_option_links_running_addon(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, mock_setup_entry
) -> None:
    """Discovery lost (e.g. flow closed): «Use the add-on» connects to the running add-on itself."""
    from unittest.mock import patch

    from homeassistant.config_entries import SOURCE_USER

    assert await async_setup_component(hass, "http", {})
    with patch("custom_components.signal_wsrpc.config_flow.is_hassio", return_value=True), patch(
        "custom_components.signal_wsrpc.config_flow.ADDON_DEFAULT_URL", _url(fake_server)
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "addon"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "link"
        result = await _run_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"] == {CONF_MODE: MODE_ADDON, CONF_URL: _url(fake_server), CONF_ACCOUNT: "+380500000000"}


async def test_scanned_pressed_before_link_finishes(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, mock_setup_entry
) -> None:
    """«Я відсканував» натиснули раніше, ніж телефон завершив — показуємо очікування, потім entry."""
    import asyncio

    assert await async_setup_component(hass, "http", {})
    server_state.link_gate = asyncio.Event()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_info(fake_server)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "link"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "link_wait"
    server_state.link_gate.set()
    result = await _run_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result


async def test_addon_discovery_ignored_when_connected_manually(hass: HomeAssistant, fake_server: str) -> None:
    """Add-on уже підключено як «external» з тим самим URL → discovery не показується знову."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="+380500000000",
        data={CONF_URL: _url(fake_server), CONF_ACCOUNT: "+380500000000"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_info(fake_server)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
