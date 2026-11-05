"""Minimal fake of SignalCliNet.WsRpcServer's WS JSON-RPC surface (for tests)."""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSCloseCode, web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


@dataclass
class FakeServerState:
    auth_enabled: bool = False
    tokens: dict[str, str] = field(default_factory=dict)  # token -> device SPKI b64
    invites: set[str] = field(default_factory=set)
    renew_token_on_prove: str | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    sockets: list[web.WebSocketResponse] = field(default_factory=list)
    link_number: str = "+380500000000"
    link_requests: list[dict[str, Any]] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)  # вже прив'язані на сервері


def _verify(spki_b64: str, nonce: str, conn_id: str, sig_b64: str) -> bool:
    key = serialization.load_der_public_key(base64.b64decode(spki_b64))
    raw = base64.b64decode(sig_b64)
    if len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        key.verify(der, (nonce + conn_id).encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def make_app(state: FakeServerState) -> web.Application:
    async def handler(request: web.Request) -> web.StreamResponse:
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        token = next((p for p in offered if p.startswith("ptzh_")), None)
        onboarding = "wsrpc-onboarding" in offered
        if state.auth_enabled and token is None and not onboarding:
            return web.Response(status=401, text="Unauthorized")  # як реальний сервер: не-браузер без токена

        ws = web.WebSocketResponse(protocols=("wsrpc-onboarding",) if onboarding else ())
        await ws.prepare(request)
        state.sockets.append(ws)
        authed = not state.auth_enabled
        pending: tuple[str, str, str] | None = None  # nonce, connId, token

        if state.auth_enabled and token is not None:
            if token not in state.tokens:
                await ws.close(code=4401, message=b"invalid")
                return ws
            nonce, conn_id = secrets.token_urlsafe(16), secrets.token_hex(8)
            pending = (nonce, conn_id, token)
            await ws.send_str(json.dumps(
                {"jsonrpc": "2.0", "method": "pop.challenge", "params": {"nonce": nonce, "connId": conn_id}}
            ))

        async def reply(req_id: Any, result: Any = None, error: dict | None = None) -> None:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
            if error is not None:
                msg["error"] = error
            else:
                msg["result"] = result
            await ws.send_str(json.dumps(msg))

        async for msg in ws:
            req = json.loads(msg.data)
            method, params, req_id = req.get("method"), req.get("params") or {}, req.get("id")

            if method == "redeemInvite":
                if params.get("code") in state.invites:
                    state.invites.discard(params["code"])
                    token = "ptzh_" + secrets.token_hex(8)
                    state.tokens[token] = params["devicePublicKey"]
                    await reply(req_id, {"identityId": "id1", "token": token})
                else:
                    await reply(req_id, error={"code": -32006, "message": "Invite is invalid or has expired."})
                continue

            if not authed:
                if method == "pop.prove" and pending is not None:
                    nonce, conn_id, token = pending
                    if _verify(state.tokens[token], nonce, conn_id, params.get("signature", "")):
                        authed = True
                        result: dict[str, Any] = {"status": "ok"}
                        if state.renew_token_on_prove:
                            result["token"] = state.renew_token_on_prove
                            state.tokens[state.renew_token_on_prove] = state.tokens[token]
                        await reply(req_id, result)
                        continue
                await ws.close(code=4401, message=b"invalid")
                break

            if method == "ping":
                await reply(req_id, "pong")
            elif method == "subscribe":
                state.subscriptions.append(params)
                await reply(req_id, len(state.subscriptions))
            elif method == "listAccounts":
                await reply(req_id, [{"number": n} for n in state.accounts])
            elif method == "startLink":
                await reply(req_id, {"sessionId": "link-1", "deviceLinkUri": "sgnl://linkdevice?uuid=x&pub_key=y"})
            elif method == "finishLink":
                state.link_requests.append(params)
                await reply(req_id, {"number": state.link_number})
            elif method == "sendTextMessage":
                state.sent.append(params)
                await reply(req_id, {"results": [], "timestamp": 1700000000000})
            else:
                await reply(req_id, error={"code": -32601, "message": "Method not found"})

        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    return app


async def push_text_message(state: FakeServerState, text: str) -> None:
    """Emit a signal.textMessage notification to every connected socket."""
    payload = {
        "subscriptionId": 1,
        "account": "+380500000000",
        "source": "+380501111111",
        "sourceNumber": "+380501111111",
        "sourceUuid": "uuid-1",
        "sourceName": "Tester",
        "timestamp": 1700000000001,
        "dataMessage": {"timestamp": 1700000000001, "message": text, "groupInfo": None},
    }
    for ws in list(state.sockets):
        if not ws.closed:
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "method": "signal.textMessage", "params": [payload]}))


async def close_all(state: FakeServerState, code: int = WSCloseCode.GOING_AWAY) -> None:
    for ws in list(state.sockets):
        if not ws.closed:
            await ws.close(code=code)
