"""Config flow for Signal (WsRpcServer)."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import timedelta
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
from homeassistant.components.http.auth import async_sign_path
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import (
    ConnectionOptions,
    SignalWsRpcConnection,
    SignalWsRpcAuthError,
    SignalWsRpcAuthRequiredError,
    SignalWsRpcCallError,
    SignalWsRpcClient,
    SignalWsRpcError,
    SignalWsRpcTlsError,
    build_ssl_context,
    generate_device_key,
    redeem_invite,
)
from .const import (
    CONF_ACCOUNT,
    CONF_CA_CERT,
    CONF_DEVICE_KEY,
    CONF_INVITE_CODE,
    CONF_MODE,
    CONF_RECEIVE,
    CONF_RECIPIENT,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    LINK_DEVICE_NAME,
    LINK_TIMEOUT,
    MODE_ADDON,
    MODE_MANAGED,
    QR_VIEW_URL,
    SUBENTRY_RECIPIENT,
)
from .manager import ManagedServerError, UnsupportedPlatformError, detect_bundle_arch, get_manager
from .views import qr_store

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


class TlsFailed(CannotConnect):
    """TLS / certificate problem."""


class InvalidAuth(Exception):
    """Token/PoP rejected."""


class AuthRequired(InvalidAuth):
    """Server has auth on and no invite was given."""


def _map_error(err: SignalWsRpcError) -> Exception:
    """Client error → flow error (keeps the reason text for the form)."""
    _LOGGER.debug("WsRpcServer validation failed: %s", err)
    if isinstance(err, SignalWsRpcAuthRequiredError):
        return AuthRequired(str(err))
    if isinstance(err, SignalWsRpcAuthError):
        return InvalidAuth(str(err))
    if isinstance(err, SignalWsRpcTlsError):
        return TlsFailed(str(err))
    return CannotConnect(str(err))


class InvalidInvite(Exception):
    """Invite code rejected."""


async def _validate(hass: HomeAssistant, data: dict[str, Any], invite_code: str | None) -> dict[str, Any]:
    """Redeem invite (optional) and open one full connection. Returns entry data."""
    url = data[CONF_URL].strip()
    if not _URL_RE.match(url):
        raise CannotConnect("URL must start with ws:// or wss://")
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
            raise CannotConnect(f"redeemInvite: {err}") from err
        except SignalWsRpcError as err:
            raise _map_error(err) from err
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
    except SignalWsRpcCallError as err:
        raise CannotConnect(f"{err.code} {err.message}") from err
    except SignalWsRpcError as err:
        raise _map_error(err) from err
    finally:
        await client.stop()
    return result


class SignalWsRpcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    _install_task: asyncio.Task[None] | None = None
    _link_task: asyncio.Task[str] | None = None
    _manager_held = False
    _qr_url: str = ""
    _addon_url: str | None = None
    _addon_name: str = "Signal WsRpcServer"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose: let the integration run the server, or connect to an existing one."""
        # HAOS / Supervised: HA у musl-контейнері, managed тут неможливий — замість нього add-on.
        first = "addon" if is_hassio(self.hass) else "managed"
        return self.async_show_menu(step_id="user", menu_options=[first, "external"])

    async def async_step_addon(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """HAOS: the server comes from the add-on, which is set up via discovery (not from here)."""
        return self.async_abort(
            reason="addon_required",
            description_placeholders={"repo_url": "https://github.com/07artem132/ha-signal-wsrpc"},
        )

    # ------------------------------------------------------------------ add-on (HAOS)

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The Signal WsRpcServer add-on announced itself via Supervisor discovery."""
        config = discovery_info.config
        url = f"ws://{config.get('host', '127.0.0.1')}:{int(config['port'])}"
        # uuid add-on'а: дедуплікує паралельні discovery-flow і поважає «Ignore».
        await self.async_set_unique_id(discovery_info.uuid)
        self._abort_if_unique_id_configured()
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_MODE) == MODE_ADDON:
                if entry.data.get(CONF_URL) != url:
                    # Порт add-on'а змінили в його налаштуваннях — оновлюємо URL і перепідключаємось.
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_URL: url})
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="already_configured")
        self._addon_url = url
        self._addon_name = discovery_info.name or self._addon_name
        self.context["title_placeholders"] = {"name": self._addon_name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm before showing the link QR."""
        if user_input is None:
            return self.async_show_form(
                step_id="hassio_confirm", description_placeholders={"addon": self._addon_name}
            )
        return await self.async_step_link()

    # ------------------------------------------------------------------ managed mode

    async def async_step_managed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Download + install the server bundle (progress)."""
        if self._install_task is None:
            try:
                await self.hass.async_add_executor_job(detect_bundle_arch)
            except UnsupportedPlatformError as err:
                return self.async_abort(reason="unsupported_platform", description_placeholders={"error": str(err)})
            self._install_task = self.hass.async_create_task(get_manager(self.hass).async_install())
        if not self._install_task.done():
            return self.async_show_progress(
                step_id="managed", progress_action="install", progress_task=self._install_task
            )
        try:
            self._install_task.result()
        except ManagedServerError as err:
            self._error_detail = str(err)
            return self.async_show_progress_done(next_step_id="managed_failed")
        return self.async_show_progress_done(next_step_id="link")

    async def async_step_managed_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Install/start/link failed."""
        return self.async_abort(reason="managed_failed", description_placeholders=self._placeholders())

    async def async_step_link(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start the server, show the link QR and wait for the scan."""
        if self._link_task is None:
            try:
                if self._addon_url is not None:
                    url = self._addon_url
                else:
                    manager = get_manager(self.hass)
                    if not self._manager_held:
                        await manager.async_acquire()
                        self._manager_held = True
                    url = manager.url
                if (existing := await self._unconfigured_account(url)) is not None:
                    # Акаунт уже прив'язаний на сервері (напр. інтеграцію видалили й додали знову):
                    # беремо його, а не лінкуємо ще один пристрій.
                    self._account = existing
                    return await self.async_step_managed_finish()
                conn, link = await self._start_link(url)
            except (ManagedServerError, SignalWsRpcError) as err:
                self._error_detail = str(err)
                return self.async_abort(reason="managed_failed", description_placeholders=self._placeholders())
            qr_store(self.hass)[self.flow_id] = await self.hass.async_add_executor_job(
                _qr_png, link["deviceLinkUri"]
            )
            self._qr_url = async_sign_path(
                self.hass, QR_VIEW_URL.format(flow_id=self.flow_id), timedelta(minutes=3)
            )
            self._link_task = self.hass.async_create_task(self._finish_link(conn, link["sessionId"]))
        if not self._link_task.done():
            return self.async_show_progress(
                step_id="link",
                progress_action="link",
                progress_task=self._link_task,
                description_placeholders={"qr_url": self._qr_url},
            )
        qr_store(self.hass).pop(self.flow_id, None)
        try:
            self._account = self._link_task.result()
        except (SignalWsRpcError, asyncio.TimeoutError) as err:
            self._error_detail = str(err) or "QR was not scanned in time"
            return self.async_show_progress_done(next_step_id="managed_failed")
        return self.async_show_progress_done(next_step_id="managed_finish")

    async def async_step_managed_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the managed entry for the freshly linked account."""
        await self.async_set_unique_id(self._account, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        if self._addon_url is not None:
            data = {CONF_MODE: MODE_ADDON, CONF_URL: self._addon_url, CONF_ACCOUNT: self._account}
        else:
            data = {CONF_MODE: MODE_MANAGED, CONF_ACCOUNT: self._account}
        return self.async_create_entry(title=f"Signal {self._account}", data=data)

    async def _unconfigured_account(self, url: str) -> str | None:
        """First account already linked on the server that has no config entry yet (else None)."""
        configured = {entry.unique_id for entry in self._async_current_entries(include_ignore=False)}
        session = async_get_clientsession(self.hass)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90
        while True:
            conn = SignalWsRpcConnection(session, ConnectionOptions(url=url))
            try:
                await conn.open()
                result = await conn.call("listAccounts", {})
                break
            except SignalWsRpcCallError as err:
                _LOGGER.debug("listAccounts недоступний (%s), переходимо до лінкування", err.code)
                return None
            except SignalWsRpcError:
                # signal-cli ще стартує (холодний старт JVM) — пробуємо ще.
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(3)
            finally:
                await conn.close()
        if isinstance(result, dict):  # масив або обгортка {"items": [...]}
            result = result.get("items") or result.get("Items")
        for item in result if isinstance(result, list) else []:
            number = item.get("number") if isinstance(item, dict) else None
            if number and number not in configured:
                return str(number)
        return None

    async def _start_link(self, url: str) -> tuple[SignalWsRpcConnection, dict[str, Any]]:
        """startLink, retrying while signal-cli is still booting (JVM cold start)."""
        session = async_get_clientsession(self.hass)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90
        while True:
            conn = SignalWsRpcConnection(session, ConnectionOptions(url=url))
            try:
                await conn.open()
                link = await conn.call("startLink", {})
                if isinstance(link, dict) and link.get("sessionId") and link.get("deviceLinkUri"):
                    return conn, link
                raise SignalWsRpcError("startLink returned no session")
            except SignalWsRpcError:
                await conn.close()
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(3)

    async def _finish_link(self, conn: SignalWsRpcConnection, session_id: str) -> str:
        try:
            result = await conn.call(
                "finishLink", {"sessionId": session_id, "deviceName": LINK_DEVICE_NAME}, timeout=LINK_TIMEOUT
            )
        finally:
            await conn.close()
        number = result.get("number") if isinstance(result, dict) else None
        if not number:
            raise SignalWsRpcError("finishLink returned no account number")
        return str(number)

    @callback
    def async_remove(self) -> None:
        """Flow ended (created, aborted or closed): drop QR and our hold on the server."""
        qr_store(self.hass).pop(self.flow_id, None)
        for task in (self._install_task, self._link_task):
            if task is not None and not task.done():
                task.cancel()
        if self._manager_held:
            self._manager_held = False
            self.hass.async_create_task(get_manager(self.hass).async_release())

    # ------------------------------------------------------------------ external server

    _error_detail: str = ""

    async def _run_validation(
        self, data: dict[str, Any], invite_code: str | None, errors: dict[str, str]
    ) -> dict[str, Any] | None:
        self._error_detail = ""
        try:
            return await _validate(self.hass, data, invite_code)
        except InvalidInvite:
            errors[CONF_INVITE_CODE] = "invalid_invite"
        except AuthRequired as err:
            errors["base"] = "auth_required"
            self._error_detail = str(err)
        except InvalidAuth as err:
            errors["base"] = "invalid_auth"
            self._error_detail = str(err)
        except TlsFailed as err:
            errors["base"] = "tls_error"
            self._error_detail = str(err)
        except CannotConnect as err:
            errors["base"] = "cannot_connect"
            self._error_detail = str(err)
        except Exception:
            _LOGGER.exception("Unexpected error during validation")
            errors["base"] = "unknown"
        return None

    def _placeholders(self) -> dict[str, str]:
        return {"error": self._error_detail or "-"}

    async def async_step_external(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Existing server: URL + sending account (+ invite when the server has auth on)."""
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
        return self.async_show_form(
            step_id="external", data_schema=schema, errors=errors, description_placeholders=self._placeholders()
        )

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
            description_placeholders=self._placeholders(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change server URL / TLS settings without losing the identity."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_MODE) in (MODE_MANAGED, MODE_ADDON):
            return self.async_abort(reason="managed_no_reconfigure")
        if user_input is not None:
            data = {**entry.data, **user_input}
            if (result := await self._run_validation(data, None, errors)) is not None:
                return self.async_update_reload_and_abort(entry, data=result)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_connection_schema(user_input or entry.data)),
            errors=errors,
            description_placeholders=self._placeholders(),
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


def _qr_png(payload: str) -> bytes:
    """QR as PNG (segno, pure Python)."""
    import segno  # noqa: PLC0415 — requirement, імпорт лише коли потрібен

    buffer = io.BytesIO()
    segno.make(payload, error="m").save(buffer, kind="png", scale=8, border=3)
    return buffer.getvalue()


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
