"""Async WebSocket JSON-RPC 2.0 client for SignalCliNet.WsRpcServer.

Deliberately free of Home Assistant imports so it can be unit-tested (and reused) on its own.

Wire contract (SignalCliNet.WsRpcServer):
* method names are camelCase (`sendTextMessage`, `subscribe`, `ping`, ...), params by name;
* server -> client notifications carry ``params`` as a one-element array (`[payload]`);
* auth (only when the server runs with ``Server:Auth:Enabled=true``): send
  ``authenticate {token}`` as the first frame, answer the ``pop.challenge {nonce, connId}``
  notification with ``pop.prove {signature}`` where signature = ECDSA P-256/SHA-256 over
  UTF-8(nonce + connId) in IEEE P1363 (raw r||s) form, base64. The ``pop.prove`` ack may carry
  a renewed ``token`` (expired-token renewal), which must be persisted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
import random
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

_LOGGER = logging.getLogger(__name__)

AUTH_CLOSE_CODE = 4401
RATE_LIMIT_CLOSE_CODE = 4429
AUTH_TIMEOUT_CLOSE_CODE = 4408

DEFAULT_TIMEOUT = 30.0
KEEPALIVE_INTERVAL = 30.0
MAX_BACKOFF = 60.0


class SignalWsRpcError(Exception):
    """Base error of this client."""


class SignalWsRpcConnectionError(SignalWsRpcError):
    """Server unreachable / connection dropped."""


class SignalWsRpcAuthError(SignalWsRpcError):
    """Token rejected / expired / revoked (WS close 4401) or PoP failed."""


class SignalWsRpcCallError(SignalWsRpcError):
    """JSON-RPC error object returned by the server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data

    @property
    def retry_after(self) -> int | None:
        """Seconds suggested by the server on -32005 (rate limit)."""
        if isinstance(self.data, dict) and isinstance(self.data.get("retry_after"), int):
            return self.data["retry_after"]
        return None


# --------------------------------------------------------------------------------------------
# Device key helpers (proof-of-possession)
# --------------------------------------------------------------------------------------------


def generate_device_key() -> str:
    """Generate a new ECDSA P-256 private key, PKCS#8 PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def public_key_spki_b64(private_key_pem: str) -> str:
    """Base64 SubjectPublicKeyInfo of the device key (what `redeemInvite` expects)."""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(spki).decode()


def sign_pop(private_key_pem: str, nonce: str, conn_id: str) -> str:
    """Sign UTF-8(nonce + connId); return base64 IEEE P1363 (r||s, 64 bytes)."""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise SignalWsRpcAuthError("Device key must be ECDSA P-256")
    der = key.sign((nonce + conn_id).encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


def build_ssl_context(url: str, verify_ssl: bool, ca_cert_pem: str | None) -> ssl.SSLContext | bool:
    """SSL settings for aiohttp: default trust, custom CA (server's auto-generated CA), or off."""
    if not url.lower().startswith("wss://"):
        return False
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    if ca_cert_pem and ca_cert_pem.strip():
        ctx.load_verify_locations(cadata=ca_cert_pem)
    return ctx


# --------------------------------------------------------------------------------------------
# Single connection
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ConnectionOptions:
    """Everything needed to open one authenticated connection."""

    url: str
    token: str | None = None
    device_key_pem: str | None = None
    ssl_context: ssl.SSLContext | bool = False
    timeout: float = DEFAULT_TIMEOUT


NotificationHandler = Callable[[str, Any], None]


class SignalWsRpcConnection:
    """One WebSocket + JSON-RPC session. Use :class:`SignalWsRpcClient` for auto-reconnect."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        options: ConnectionOptions,
        on_notification: NotificationHandler | None = None,
    ) -> None:
        self._session = session
        self._options = options
        self._on_notification = on_notification
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._challenge: asyncio.Future[tuple[str, str]] | None = None
        self._closed = asyncio.Event()
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.renewed_token: str | None = None

    @property
    def closed(self) -> asyncio.Event:
        """Set once the socket is gone."""
        return self._closed

    async def open(self) -> None:
        """Connect and (if a token is configured) authenticate with proof-of-possession."""
        try:
            self._ws = await self._session.ws_connect(
                self._options.url,
                ssl=self._options.ssl_context,
                heartbeat=None,  # сервер сам шле app-level heartbeat; WS-ping не потрібен
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
                max_msg_size=0,
            )
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
            raise SignalWsRpcConnectionError(f"Cannot connect: {err}") from err

        loop = asyncio.get_running_loop()
        self._challenge = loop.create_future()
        self._reader = asyncio.create_task(self._read_loop(), name="signal_wsrpc_reader")

        if self._options.token:
            await self._authenticate()

    async def _authenticate(self) -> None:
        assert self._ws is not None and self._challenge is not None
        if not self._options.device_key_pem:
            raise SignalWsRpcAuthError("Token configured without a device key")
        # Fallback-шлях (перший кадр authenticate), а не субпротокол: лише так сервер дозволяє
        # renewal простроченого токена через PoP.
        await self._send_raw(
            {"jsonrpc": "2.0", "method": "authenticate", "params": {"token": self._options.token}}
        )
        try:
            nonce, conn_id = await asyncio.wait_for(
                asyncio.shield(self._challenge), self._options.timeout
            )
        except asyncio.TimeoutError as err:
            await self.close()
            raise SignalWsRpcAuthError("No pop.challenge from server") from err
        except SignalWsRpcError:
            await self.close()
            raise

        signature = sign_pop(self._options.device_key_pem, nonce, conn_id)
        ack = await self.call("pop.prove", {"signature": signature})
        if isinstance(ack, dict):
            if ack.get("status") != "ok":
                await self.close()
                raise SignalWsRpcAuthError("pop.prove rejected")
            if isinstance(ack.get("token"), str):
                self.renewed_token = ack["token"]

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        """Invoke a JSON-RPC method and await its result."""
        if self._ws is None or self._ws.closed:
            raise SignalWsRpcConnectionError("Not connected")
        req_id = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send_raw(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
            )
            return await asyncio.wait_for(fut, timeout or self._options.timeout)
        except asyncio.TimeoutError as err:
            raise SignalWsRpcConnectionError(f"Timeout waiting for {method}") from err
        finally:
            self._pending.pop(req_id, None)

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None
        try:
            await self._ws.send_str(json.dumps(payload, separators=(",", ":")))
        except (aiohttp.ClientError, ConnectionResetError, RuntimeError) as err:
            raise SignalWsRpcConnectionError(f"Send failed: {err}") from err

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raw = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8", "replace")
                    self._dispatch(raw)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except (aiohttp.ClientError, asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self.close_code = self._ws.close_code
            self._fail_pending()
            self._closed.set()

    def _fail_pending(self) -> None:
        if self.close_code == AUTH_CLOSE_CODE:
            err: SignalWsRpcError = SignalWsRpcAuthError("Server rejected the token (4401)")
        else:
            err = SignalWsRpcConnectionError(f"Connection closed (code {self.close_code})")
        if self._challenge is not None and not self._challenge.done():
            self._challenge.set_exception(err)
            self._challenge.exception()  # mark retrieved
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    def _dispatch(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON frame")
            return
        frames = frame if isinstance(frame, list) else [frame]
        for item in frames:
            if not isinstance(item, dict):
                continue
            if "method" in item and "id" not in item:
                self._handle_notification(item["method"], item.get("params"))
                continue
            req_id = item.get("id")
            fut = self._pending.get(req_id) if isinstance(req_id, int) else None
            if fut is None or fut.done():
                continue
            if "error" in item and item["error"] is not None:
                error = item["error"] or {}
                fut.set_exception(
                    SignalWsRpcCallError(
                        int(error.get("code", -32000)), str(error.get("message", "")), error.get("data")
                    )
                )
            else:
                fut.set_result(item.get("result"))

    def _handle_notification(self, method: str, params: Any) -> None:
        payload = params[0] if isinstance(params, list) and len(params) == 1 else params
        if method == "pop.challenge":
            if self._challenge is not None and not self._challenge.done() and isinstance(payload, dict):
                self._challenge.set_result((str(payload.get("nonce", "")), str(payload.get("connId", ""))))
            return
        if self._on_notification is not None:
            try:
                self._on_notification(method, payload)
            except Exception:  # noqa: BLE001 — колбек не має вбити reader
                _LOGGER.exception("Notification handler failed for %s", method)

    async def close(self) -> None:
        """Close the socket and stop the reader."""
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._reader is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._reader, 5)
        self._closed.set()


async def redeem_invite(
    session: aiohttp.ClientSession,
    url: str,
    ssl_context: ssl.SSLContext | bool,
    invite_code: str,
    device_key_pem: str,
) -> str:
    """Redeem a one-time invite; returns the new identity's access token (secret!)."""
    conn = SignalWsRpcConnection(session, ConnectionOptions(url=url, ssl_context=ssl_context))
    await conn.open()
    try:
        result = await conn.call(
            "redeemInvite",
            {"code": invite_code, "devicePublicKey": public_key_spki_b64(device_key_pem)},
        )
    finally:
        await conn.close()
    token = result.get("token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        raise SignalWsRpcError("redeemInvite returned no token")
    return token


# --------------------------------------------------------------------------------------------
# Long-lived client with reconnect
# --------------------------------------------------------------------------------------------


class SignalWsRpcClient:
    """Keeps one connection alive: reconnect with jittered backoff, keepalive ping, re-subscribe."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        options: ConnectionOptions,
        *,
        account: str,
        subscribe_event_types: int = 0,
        on_event: Callable[[str, Any], None] | None = None,
        on_state: Callable[[bool], None] | None = None,
        on_token_renewed: Callable[[str], Awaitable[None]] | None = None,
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._options = options
        self.account = account
        self._subscribe_event_types = subscribe_event_types
        self._on_event = on_event
        self._on_state = on_state
        self._on_token_renewed = on_token_renewed
        self._on_auth_failed = on_auth_failed
        self._conn: SignalWsRpcConnection | None = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stopping = False
        self.connected = False

    async def connect_once(self) -> None:
        """Open the first connection (raises on failure, for config validation / setup)."""
        await self._open()

    def start(self) -> None:
        """Start the background supervisor."""
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="signal_wsrpc_supervisor")

    async def stop(self) -> None:
        """Stop reconnecting and close the socket."""
        self._stopping = True
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self._set_state(False)

    async def send_text(self, recipients: list[str], message: str, message_id: str | None = None) -> Any:
        """`sendTextMessage` from the configured account."""
        params: dict[str, Any] = {"account": self.account, "recipients": recipients, "message": message}
        if message_id:
            params["messageId"] = message_id
        return await self.call("sendTextMessage", params)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call over the current connection; waits briefly for a reconnect in progress."""
        if self._conn is None or self._conn.closed.is_set():
            try:
                await asyncio.wait_for(self._connected.wait(), 10)
            except asyncio.TimeoutError as err:
                raise SignalWsRpcConnectionError("Not connected to WsRpcServer") from err
        assert self._conn is not None
        return await self._conn.call(method, params)

    async def _open(self) -> None:
        conn = SignalWsRpcConnection(self._session, self._options, self._handle_notification)
        await conn.open()
        try:
            if conn.renewed_token and self._on_token_renewed is not None:
                self._options.token = conn.renewed_token
                await self._on_token_renewed(conn.renewed_token)
            await conn.call("ping")
            if self._subscribe_event_types:
                await conn.call(
                    "subscribe", {"account": self.account, "eventTypes": self._subscribe_event_types}
                )
        except BaseException:
            await conn.close()
            raise
        self._conn = conn
        self._set_state(True)

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            if self._conn is None or self._conn.closed.is_set():
                try:
                    await self._open()
                    attempt = 0
                except SignalWsRpcAuthError:
                    _LOGGER.warning("WsRpcServer відхилив токен — потрібна повторна автентифікація")
                    self._set_state(False)
                    if self._on_auth_failed is not None:
                        self._on_auth_failed()
                    return
                except SignalWsRpcError as err:
                    attempt += 1
                    delay = min(MAX_BACKOFF, 2 ** min(attempt, 6)) * (0.5 + random.random())
                    _LOGGER.debug("Reconnect failed (%s); retry in %.1fs", err, delay)
                    self._set_state(False)
                    await asyncio.sleep(delay)
                    continue

            assert self._conn is not None
            closed_wait = asyncio.create_task(self._conn.closed.wait())
            done, _ = await asyncio.wait({closed_wait}, timeout=KEEPALIVE_INTERVAL)
            if not done:
                closed_wait.cancel()
                try:
                    await self._conn.call("ping", timeout=10)
                except SignalWsRpcError:
                    _LOGGER.debug("Keepalive ping failed; reconnecting")
                    await self._conn.close()
                continue

            code = self._conn.close_code
            self._set_state(False)
            if code == AUTH_CLOSE_CODE:
                if self._on_auth_failed is not None:
                    self._on_auth_failed()
                return
            # 1000 idle-timeout → одразу; решта (у т.ч. 4429) → backoff у гілці open.
            if code != 1000:
                await asyncio.sleep(1 + random.random())

    def _handle_notification(self, method: str, payload: Any) -> None:
        if method == "heartbeat":
            return
        if self._on_event is not None:
            self._on_event(method, payload)

    def _set_state(self, connected: bool) -> None:
        if connected:
            self._connected.set()
        else:
            self._connected.clear()
        if connected != self.connected:
            self.connected = connected
            if self._on_state is not None:
                self._on_state(connected)
