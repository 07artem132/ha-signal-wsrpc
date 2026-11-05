"""Config flow for Signal (WsRpcServer)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import (
    ConnectionOptions,
    SignalWsRpcAuthError,
    SignalWsRpcCallError,
    SignalWsRpcClient,
    SignalWsRpcError,
    build_ssl_context,
    generate_device_key,
    redeem_invite,
)
from .const import (
    CONF_ACCOUNT,
    CONF_CA_CERT,
    CONF_DEVICE_KEY,
    CONF_INVITE_CODE,
    CONF_RECEIVE,
    CONF_RECIPIENT,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    SUBENTRY_RECIPIENT,
)

_LOGGER = logging.getLogger(__name__)

INVALID_INVITE_CODE = -32006
_URL_RE = re.compile(r"^wss?://", re.IGNORECASE)

MULTILINE = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _connection_schema(defaults: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_URL, default=defaults.get(CONF_URL, "ws://127.0.0.1:9000")): str,
        vol.Optional(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
        vol.Optional(
            CONF_CA_CERT, description={"suggested_value": defaults.get(CONF_CA_CERT)}
        ): MULTILINE,
    }


class CannotConnect(Exception):
    """Server unreachable."""


class InvalidAuth(Exception):
    """Token/PoP rejected."""


class InvalidInvite(Exception):
    """Invite code rejected."""


async def _validate(hass: HomeAssistant, data: dict[str, Any], invite_code: str | None) -> dict[str, Any]:
    """Redeem invite (optional) and open one full connection. Returns entry data."""
    url = data[CONF_URL].strip()
    if not _URL_RE.match(url):
        raise CannotConnect
    ssl_context = await hass.async_add_executor_job(
        build_ssl_context, url, data.get(CONF_VERIFY_SSL, True), data.get(CONF_CA_CERT)
    )
    session = async_get_clientsession(hass)
    result = {**data, CONF_URL: url}

    if invite_code:
        device_key = await hass.async_add_executor_job(generate_device_key)
        try:
            token = await redeem_invite(session, url, ssl_context, invite_code.strip(), device_key)
        except SignalWsRpcCallError as err:
            if err.code == INVALID_INVITE_CODE:
                raise InvalidInvite from err
            raise CannotConnect from err
        except SignalWsRpcAuthError as err:
            raise InvalidAuth from err
        except SignalWsRpcError as err:
            raise CannotConnect from err
        result[CONF_TOKEN] = token
        result[CONF_DEVICE_KEY] = device_key

    client = SignalWsRpcClient(
        session,
        ConnectionOptions(
            url=url,
            token=result.get(CONF_TOKEN),
            device_key_pem=result.get(CONF_DEVICE_KEY),
            ssl_context=ssl_context,
        ),
        account=result[CONF_ACCOUNT],
    )
    try:
        await client.connect_once()
    except SignalWsRpcAuthError as err:
        raise InvalidAuth from err
    except SignalWsRpcError as err:
        raise CannotConnect from err
    finally:
        await client.stop()
    return result


class SignalWsRpcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def _run_validation(
        self, data: dict[str, Any], invite_code: str | None, errors: dict[str, str]
    ) -> dict[str, Any] | None:
        try:
            return await _validate(self.hass, data, invite_code)
        except InvalidInvite:
            errors[CONF_INVITE_CODE] = "invalid_invite"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during validation")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """First step: server + sending account (+ invite when the server has auth on)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            account = user_input[CONF_ACCOUNT].strip()
            await self.async_set_unique_id(account)
            self._abort_if_unique_id_configured()
            invite = user_input.pop(CONF_INVITE_CODE, None)
            data = {**user_input, CONF_ACCOUNT: account}
            if (result := await self._run_validation(data, invite, errors)) is not None:
                return self.async_create_entry(title=f"Signal {account}", data=result)

        schema = vol.Schema(
            {
                **_connection_schema(user_input or {}),
                vol.Required(CONF_ACCOUNT, default=(user_input or {}).get(CONF_ACCOUNT, "")): str,
                vol.Optional(CONF_INVITE_CODE): PASSWORD,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Token rejected/revoked: ask for a new invite."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Redeem a fresh invite for the existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            base = {k: v for k, v in entry.data.items() if k not in (CONF_TOKEN, CONF_DEVICE_KEY)}
            if (result := await self._run_validation(base, user_input[CONF_INVITE_CODE], errors)) is not None:
                return self.async_update_reload_and_abort(entry, data=result)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_INVITE_CODE): PASSWORD}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change server URL / TLS settings without losing the identity."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            if (result := await self._run_validation(data, None, errors)) is not None:
                return self.async_update_reload_and_abort(entry, data=result)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_connection_schema(user_input or entry.data)),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SignalWsRpcOptionsFlow:
        """Options."""
        return SignalWsRpcOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Recipients are subentries — each becomes a notify entity."""
        return {SUBENTRY_RECIPIENT: RecipientSubentryFlow}


class SignalWsRpcOptionsFlow(OptionsFlow):
    """Options: receive incoming messages as HA events."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_RECEIVE, default=self.config_entry.options.get(CONF_RECEIVE, True)
                    ): bool,
                }
            ),
        )


class RecipientSubentryFlow(ConfigSubentryFlow):
    """Add a recipient (phone number, UUID or group id)."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add recipient."""
        errors: dict[str, str] = {}
        if user_input is not None:
            recipient = user_input[CONF_RECIPIENT].strip()
            entry = self._get_entry()
            if any(s.data.get(CONF_RECIPIENT) == recipient for s in entry.subentries.values()):
                errors[CONF_RECIPIENT] = "already_configured"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip() or recipient,
                    data={CONF_RECIPIENT: recipient},
                    unique_id=recipient,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME): str, vol.Required(CONF_RECIPIENT): str}
            ),
            errors=errors,
        )
