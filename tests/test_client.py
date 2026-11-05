"""Client-level tests against the fake server (no HA)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from custom_components.signal_wsrpc.client import (
    ConnectionOptions,
    SignalWsRpcAuthError,
    SignalWsRpcCallError,
    SignalWsRpcClient,
    SignalWsRpcConnection,
    generate_device_key,
    redeem_invite,
)

from .fake_server import FakeServerState, close_all, push_text_message


async def test_send_without_auth(fake_server: str, server_state: FakeServerState) -> None:
    async with aiohttp.ClientSession() as session:
        client = SignalWsRpcClient(session, ConnectionOptions(url=fake_server), account="+380500000000")
        await client.connect_once()
        result = await client.send_text(["+380501111111"], "hi", message_id="k1")
        await client.stop()
    assert result["timestamp"] == 1700000000000
    assert server_state.sent == [
        {"account": "+380500000000", "recipients": ["+380501111111"], "message": "hi", "messageId": "k1"}
    ]


async def test_invite_then_pop_auth(fake_server: str, server_state: FakeServerState) -> None:
    server_state.auth_enabled = True
    server_state.invites.add("INV-1")
    key = generate_device_key()
    async with aiohttp.ClientSession() as session:
        token = await redeem_invite(session, fake_server, False, "INV-1", key)
        assert token.startswith("ptzh_")
        conn = SignalWsRpcConnection(session, ConnectionOptions(url=fake_server, token=token, device_key_pem=key))
        await conn.open()
        assert await conn.call("ping") == "pong"
        await conn.close()


async def test_wrong_key_is_auth_error(fake_server: str, server_state: FakeServerState) -> None:
    server_state.auth_enabled = True
    server_state.invites.add("INV-1")
    async with aiohttp.ClientSession() as session:
        token = await redeem_invite(session, fake_server, False, "INV-1", generate_device_key())
        conn = SignalWsRpcConnection(
            session, ConnectionOptions(url=fake_server, token=token, device_key_pem=generate_device_key())
        )
        with pytest.raises(SignalWsRpcAuthError):
            await conn.open()


async def test_invalid_invite(fake_server: str) -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(SignalWsRpcCallError) as err:
            await redeem_invite(session, fake_server, False, "nope", generate_device_key())
    assert err.value.code == -32006


async def test_token_renewal_reported(fake_server: str, server_state: FakeServerState) -> None:
    server_state.auth_enabled = True
    server_state.invites.add("INV-1")
    server_state.renew_token_on_prove = "ptzh_renewed"
    key = generate_device_key()
    renewed: list[str] = []

    async def on_renew(tok: str) -> None:
        renewed.append(tok)

    async with aiohttp.ClientSession() as session:
        token = await redeem_invite(session, fake_server, False, "INV-1", key)
        client = SignalWsRpcClient(
            session,
            ConnectionOptions(url=fake_server, token=token, device_key_pem=key),
            account="+380500000000",
            on_token_renewed=on_renew,
        )
        await client.connect_once()
        await client.stop()
    assert renewed == ["ptzh_renewed"]


async def test_events_and_reconnect(fake_server: str, server_state: FakeServerState) -> None:
    events: list[tuple[str, dict]] = []
    states: list[bool] = []
    async with aiohttp.ClientSession() as session:
        client = SignalWsRpcClient(
            session,
            ConnectionOptions(url=fake_server),
            account="+380500000000",
            subscribe_event_types=1,
            on_event=lambda m, p: events.append((m, p)),
            on_state=states.append,
        )
        await client.connect_once()
        client.start()
        assert server_state.subscriptions == [{"account": "+380500000000", "eventTypes": 1}]

        await push_text_message(server_state, "hello")
        await asyncio.sleep(0.05)
        assert events[0][0] == "signal.textMessage"
        assert events[0][1]["dataMessage"]["message"] == "hello"

        await close_all(server_state)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(server_state.subscriptions) == 2 and client.connected:
                break
        assert len(server_state.subscriptions) == 2  # re-subscribed after reconnect
        assert states[:3] == [True, False, True]
        await client.stop()
