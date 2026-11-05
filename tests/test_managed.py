"""Managed mode: bundle install/supervision + QR-link config flow."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.signal_wsrpc import manager as mgr
from custom_components.signal_wsrpc.const import CONF_ACCOUNT, CONF_MODE, DOMAIN, MODE_MANAGED
from custom_components.signal_wsrpc.views import qr_store

from .fake_server import FakeServerState

# Фейковий «сервер»: слухає Server__Port як справжній бінарник.
FAKE_EXE = """#!/bin/sh
exec python3 -I -c 'import os,socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(os.environ["Server__Port"]))); s.listen()
assert os.environ["Server__Auth__Enabled"] == "false"
while True: c,_=s.accept(); c.close()'
"""


def _bundle(extra: dict[str, bytes] | None = None) -> bytes:
    files = {mgr.EXECUTABLE: FAKE_EXE.encode(), "jre/bin/java": b"#!/bin/sh\n", **(extra or {})}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
async def release_server(socket_enabled):
    """Serves SHA256SUMS + the x64 bundle like a GitHub release."""
    bundle = _bundle()
    name = "SignalCliNet.WsRpcServer-9.9.9-linux-x64-bundle.tar.gz"
    files = {
        "SHA256SUMS": f"{hashlib.sha256(bundle).hexdigest()}  {name}\n".encode(),
        name: bundle,
    }

    async def handler(request: web.Request) -> web.Response:
        data = files.get(request.match_info["name"])
        return web.Response(body=data) if data is not None else web.Response(status=404)

    app = web.Application()
    app.router.add_get("/{name}", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    yield f"http://127.0.0.1:{server.port}", files
    await server.close()


def test_detect_arch_rejects_musl() -> None:
    # musl не знає CS_GNU_LIBC_VERSION → os.confstr кидає ValueError.
    with patch.object(mgr.platform, "system", return_value="Linux"), patch.object(
        mgr.os, "confstr", side_effect=ValueError
    ), patch.object(mgr.platform, "libc_ver", return_value=("", "")), patch.object(
        mgr.os.path, "exists", return_value=True
    ):
        with pytest.raises(mgr.UnsupportedPlatformError):
            mgr.detect_bundle_arch()


def test_detect_arch_glibc_when_libc_ver_is_blind() -> None:
    # Деякі збірки Python (uv / python-build-standalone): libc_ver() == ('', ''), хоча хост glibc.
    with patch.object(mgr.platform, "system", return_value="Linux"), patch.object(
        mgr.os, "confstr", return_value="glibc 2.36"
    ), patch.object(mgr.platform, "libc_ver", return_value=("", "")), patch.object(
        mgr.platform, "machine", return_value="x86_64"
    ):
        assert mgr.detect_bundle_arch() == "x64"


@pytest.mark.parametrize(("machine", "arch"), [("x86_64", "x64"), ("aarch64", "arm64")])
def test_detect_arch(machine: str, arch: str) -> None:
    with patch.object(mgr.platform, "system", return_value="Linux"), patch.object(
        mgr.platform, "libc_ver", return_value=("glibc", "2.36")
    ), patch.object(mgr.os.path, "exists", return_value=False), patch.object(
        mgr.platform, "machine", return_value=machine
    ):
        assert mgr.detect_bundle_arch() == arch


async def test_install_and_run(hass: HomeAssistant, release_server, tmp_path: Path) -> None:
    base, _ = release_server
    hass.config.config_dir = str(tmp_path)
    server = mgr.ManagedServer(hass, version="9.9.9")
    with patch.object(mgr, "RELEASE_BASE_URL", base), patch.object(mgr, "detect_bundle_arch", return_value="x64"):
        await server.async_install()
    assert (server.install_dir / ".installed").read_text() == "9.9.9"
    assert (server.install_dir / mgr.EXECUTABLE).stat().st_mode & 0o111

    await server.async_acquire()
    try:
        assert server.url.startswith("ws://127.0.0.1:")
        port = server.port
        # Процес падає → supervisor піднімає його знову на тому ж порту.
        server._proc.kill()
        server._started.clear()
        await server._started.wait()
        assert server.port == port
    finally:
        await server.async_release(idle_delay=0)
    assert server._proc is None and server._supervisor is None


async def test_install_checksum_mismatch(hass: HomeAssistant, release_server, tmp_path: Path) -> None:
    base, files = release_server
    files["SHA256SUMS"] = b"0" * 64 + b"  SignalCliNet.WsRpcServer-9.9.9-linux-x64-bundle.tar.gz\n"
    hass.config.config_dir = str(tmp_path)
    server = mgr.ManagedServer(hass, version="9.9.9")
    with patch.object(mgr, "RELEASE_BASE_URL", base), patch.object(mgr, "detect_bundle_arch", return_value="x64"):
        with pytest.raises(mgr.ManagedServerError, match="Checksum"):
            await server.async_install()
    assert not server.install_dir.exists()


class _FakeManager:
    def __init__(self, url: str) -> None:
        self.url = url
        self.users = 0

    async def async_install(self, progress=None) -> None:
        return None

    async def async_acquire(self) -> None:
        self.users += 1

    async def async_release(self) -> None:
        self.users -= 1


async def test_managed_flow_links_account(
    hass: HomeAssistant, fake_server: str, server_state: FakeServerState, mock_setup_entry
) -> None:
    assert await async_setup_component(hass, "http", {})
    fake = _FakeManager(fake_server)
    with patch("custom_components.signal_wsrpc.config_flow.get_manager", return_value=fake), patch(
        "custom_components.signal_wsrpc.config_flow.detect_bundle_arch", return_value="x64"
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "managed"})
        flow_id = result["flow_id"]
        # install → link (QR) → finish; кожен progress-крок завершується сам.
        for _ in range(8):
            if result["type"] is FlowResultType.FORM and result["step_id"] == "link":
                result = await hass.config_entries.flow.async_configure(flow_id, {})
                continue
            if result["type"] is not FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(flow_id)
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"] == {CONF_MODE: MODE_MANAGED, CONF_ACCOUNT: "+380500000000"}
    assert result["result"].unique_id == "+380500000000"
    assert server_state.link_requests == [{"sessionId": "link-1", "deviceName": "Home Assistant"}]
    assert flow_id not in qr_store(hass)
    await hass.async_block_till_done()
    assert fake.users == 0  # flow відпустив сервер; entry тримає його сам у async_setup_entry


async def test_managed_flow_unsupported_platform(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.signal_wsrpc.config_flow.detect_bundle_arch",
        side_effect=mgr.UnsupportedPlatformError("musl"),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "managed"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_platform"


async def test_qr_view_serves_png(hass: HomeAssistant, hass_client) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    qr_store(hass)["abc"] = b"\x89PNG"
    client = await hass_client()
    resp = await client.get("/api/signal_wsrpc/link_qr/abc")
    assert resp.status == 200 and await resp.read() == b"\x89PNG"
    assert (await client.get("/api/signal_wsrpc/link_qr/nope")).status == 404



async def test_release_keeps_server_for_handover(hass: HomeAssistant) -> None:
    """Flow → entry handover must not restart the JVM; a real idle period stops it."""
    import asyncio

    server = mgr.ManagedServer(hass)
    server._users = 1
    with patch.object(server, "async_stop") as stop:
        await server.async_release(idle_delay=0.05)
        server._users += 1  # entry підхопив сервер до таймера (як async_acquire)
        server._idle_stop.cancel()
        server._idle_stop = None
        await asyncio.sleep(0.1)
        assert stop.call_count == 0
        await server.async_release(idle_delay=0.05)
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()
        assert stop.call_count == 1


async def test_managed_entry_setup_and_unload(hass: HomeAssistant, fake_server: str) -> None:
    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    fake = _FakeManager(fake_server)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_MODE: MODE_MANAGED, CONF_ACCOUNT: "+380500000000"}, unique_id="+380500000000"
    )
    entry.add_to_hass(hass)
    with patch("custom_components.signal_wsrpc.get_manager", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED and fake.users == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert fake.users == 0
