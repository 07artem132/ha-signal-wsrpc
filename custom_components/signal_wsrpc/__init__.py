"""Signal messenger for Home Assistant via SignalCliNet.WsRpcServer (WebSocket JSON-RPC)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .client import (
    ConnectionOptions,
    SignalWsRpcAuthError,
    SignalWsRpcCallError,
    SignalWsRpcClient,
    SignalWsRpcError,
    build_ssl_context,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_MESSAGE,
    ATTR_RECIPIENTS,
    CONF_ACCOUNT,
    CONF_CA_CERT,
    CONF_DEVICE_KEY,
    CONF_RECEIVE,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    EVENT_MESSAGE_RECEIVED,
    EVENT_TYPES_TEXT_MESSAGES,
    SERVICE_SEND_MESSAGE,
    SIGNAL_CONNECTION_STATE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.NOTIFY]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_RECIPIENTS): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1)),
        vol.Required(ATTR_MESSAGE): cv.string,
    }
)


@dataclass(slots=True)
class SignalRuntimeData:
    """Per-entry runtime state."""

    client: SignalWsRpcClient
    reload_key: tuple[Any, ...]


type SignalConfigEntry = ConfigEntry[SignalRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-wide actions."""

    async def _send_message(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        result = await async_send(entry, call.data[ATTR_RECIPIENTS], call.data[ATTR_MESSAGE])
        if call.return_response:
            return {"timestamp": result.get("timestamp") if isinstance(result, dict) else None}
        return None

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        _send_message,
        schema=SEND_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _resolve_entry(hass: HomeAssistant, entry_id: str | None) -> SignalConfigEntry:
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        raise ServiceValidationError("No loaded Signal (WsRpcServer) entry found")
    if len(entries) > 1:
        raise ServiceValidationError(
            "Several Signal accounts are configured — pass config_entry_id"
        )
    return entries[0]


async def async_send(entry: SignalConfigEntry, recipients: list[str], message: str) -> Any:
    """Send a text message; maps client errors to HA errors (no message bodies in logs)."""
    try:
        return await entry.runtime_data.client.send_text(recipients, message)
    except SignalWsRpcCallError as err:
        hint = f" (retry after {err.retry_after}s)" if err.retry_after else ""
        raise HomeAssistantError(f"Signal send failed: {err.code} {err.message}{hint}") from err
    except SignalWsRpcError as err:
        raise HomeAssistantError(f"Signal send failed: {err}") from err


async def async_setup_entry(hass: HomeAssistant, entry: SignalConfigEntry) -> bool:
    """Connect to WsRpcServer and set up platforms."""
    data = entry.data
    ssl_context = await hass.async_add_executor_job(
        build_ssl_context, data[CONF_URL], data.get(CONF_VERIFY_SSL, True), data.get(CONF_CA_CERT)
    )
    options = ConnectionOptions(
        url=data[CONF_URL],
        token=data.get(CONF_TOKEN),
        device_key_pem=data.get(CONF_DEVICE_KEY),
        ssl_context=ssl_context,
    )

    @callback
    def _on_event(method: str, payload: Any) -> None:
        if method == "signal.textMessage" and isinstance(payload, dict):
            hass.bus.async_fire(EVENT_MESSAGE_RECEIVED, _text_event_data(payload))
        elif method in ("bot.paused", "bot.resumed"):
            _LOGGER.info("WsRpcServer: %s", method)

    @callback
    def _on_state(connected: bool) -> None:
        async_dispatcher_send(hass, SIGNAL_CONNECTION_STATE.format(entry.entry_id), connected)

    async def _on_token_renewed(token: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: token})

    @callback
    def _on_auth_failed() -> None:
        entry.async_start_reauth(hass)

    client = SignalWsRpcClient(
        async_get_clientsession(hass),
        options,
        account=data[CONF_ACCOUNT],
        subscribe_event_types=EVENT_TYPES_TEXT_MESSAGES if entry.options.get(CONF_RECEIVE, True) else 0,
        on_event=_on_event,
        on_state=_on_state,
        on_token_renewed=_on_token_renewed,
        on_auth_failed=_on_auth_failed,
    )

    try:
        await client.connect_once()
    except SignalWsRpcAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SignalWsRpcError as err:
        raise ConfigEntryNotReady(f"WsRpcServer unavailable: {err}") from err

    client.start()
    entry.runtime_data = SignalRuntimeData(client=client, reload_key=_reload_key(entry))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _text_event_data(payload: dict[str, Any]) -> dict[str, Any]:
    data_message = payload.get("dataMessage") or {}
    group = data_message.get("groupInfo") or {}
    return {
        "account": payload.get("account"),
        "source": payload.get("sourceNumber") or payload.get("sourceUuid") or payload.get("source"),
        "source_uuid": payload.get("sourceUuid"),
        "source_name": payload.get("sourceName"),
        "message": data_message.get("message"),
        "group_id": group.get("groupId"),
        "group_name": group.get("groupName"),
        "timestamp": payload.get("timestamp"),
    }


def _reload_key(entry: SignalConfigEntry) -> tuple[Any, ...]:
    # Токен ротується in-place (renewal) — це НЕ привід перезавантажувати entry.
    data = {k: v for k, v in entry.data.items() if k != CONF_TOKEN}
    return (
        tuple(sorted(data.items())),
        tuple(sorted(entry.options.items())),
        tuple(sorted((sid, tuple(sorted(s.data.items()))) for sid, s in entry.subentries.items())),
    )


async def _async_reload(hass: HomeAssistant, entry: SignalConfigEntry) -> None:
    if _reload_key(entry) != entry.runtime_data.reload_key:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SignalConfigEntry) -> bool:
    """Unload platforms and close the connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.stop()
    return unloaded
