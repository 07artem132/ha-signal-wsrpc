"""Managed mode: download, verify and supervise a local SignalCliNet.WsRpcServer.

The integration fetches an all-in-one release bundle (server + Temurin JRE 25 + libsignal for the
host arch, built by WsRpcServer's release workflow), verifies its SHA-256 against the release's
``SHA256SUMS`` and runs it as a subprocess on ``127.0.0.1`` with auth disabled (nothing is exposed
to the network). Only Linux glibc x86_64 / aarch64 is supported — libsignal has no musl builds,
so Home Assistant OS / the HA container image (Alpine) must use the add-on or an external server.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import platform
import shutil
import socket
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

import aiohttp

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, MANAGED_SERVER_VERSION, RELEASE_BASE_URL

_LOGGER = logging.getLogger(__name__)

EXECUTABLE = "SignalCliNet.WsRpcServer"
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=1800, sock_read=120)
RESTART_BACKOFF_MAX = 60.0
STOP_TIMEOUT = 20.0
IDLE_STOP_DELAY = 30.0  # flow → entry передають сервер одне одному без перезапуску JVM


class ManagedServerError(Exception):
    """Install / start failure (message is user-facing)."""


class UnsupportedPlatformError(ManagedServerError):
    """Host can't run the bundle (musl, non-Linux, unsupported arch)."""


def detect_bundle_arch() -> str:
    """Return 'x64' / 'arm64' or raise UnsupportedPlatformError."""
    if platform.system() != "Linux":
        raise UnsupportedPlatformError(f"{platform.system()} is not supported (Linux only)")
    if os.path.exists("/etc/alpine-release") or platform.libc_ver()[0] != "glibc":
        raise UnsupportedPlatformError(
            "musl/Alpine (Home Assistant OS or the HA container) is not supported in managed mode: "
            "use the add-on or connect to an external server"
        )
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise UnsupportedPlatformError(f"CPU architecture {machine} is not supported")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ManagedServer:
    """One local WsRpcServer per Home Assistant instance."""

    def __init__(self, hass: HomeAssistant, version: str = MANAGED_SERVER_VERSION) -> None:
        self.hass = hass
        self.version = version
        self.base_dir = Path(hass.config.path(DOMAIN))
        self.install_dir = self.base_dir / "server" / version
        self.data_dir = self.base_dir / "data"
        self.port: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False
        self._users = 0
        self._started = asyncio.Event()
        self._idle_stop: asyncio.TimerHandle | None = None

    # ---------------------------------------------------------------- install

    @property
    def url(self) -> str:
        if self.port is None:
            raise ManagedServerError("Managed server is not running")
        return f"ws://127.0.0.1:{self.port}/"

    def _is_installed(self) -> bool:
        return (self.install_dir / ".installed").exists() and (self.install_dir / EXECUTABLE).exists()

    async def async_install(self, progress: Callable[[float], None] | None = None) -> None:
        """Download + verify + extract the bundle if this version isn't installed yet."""
        if await self.hass.async_add_executor_job(self._is_installed):
            return
        arch = await self.hass.async_add_executor_job(detect_bundle_arch)
        name = f"SignalCliNet.WsRpcServer-{self.version}-linux-{arch}-bundle.tar.gz"
        base = RELEASE_BASE_URL.format(version=self.version)
        session = async_get_clientsession(self.hass)

        expected = await self._fetch_expected_sha(session, f"{base}/SHA256SUMS", name)
        await self.hass.async_add_executor_job(self.base_dir.mkdir, 0o700, True, True)
        tmp_fd, tmp_name = await self.hass.async_add_executor_job(
            lambda: tempfile.mkstemp(prefix="bundle-", suffix=".tar.gz", dir=self.base_dir)
        )
        os.close(tmp_fd)
        try:
            actual = await self._download(session, f"{base}/{name}", Path(tmp_name), progress)
            if actual != expected:
                raise ManagedServerError(f"Checksum mismatch for {name}")
            await self.hass.async_add_executor_job(self._extract, Path(tmp_name))
        finally:
            with contextlib.suppress(FileNotFoundError):
                await self.hass.async_add_executor_job(os.remove, tmp_name)
        _LOGGER.info("Встановлено WsRpcServer %s (%s)", self.version, arch)

    async def _fetch_expected_sha(self, session: aiohttp.ClientSession, url: str, name: str) -> str:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise ManagedServerError(f"Cannot download {url}: {err}") from err
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                return parts[0].lower()
        raise ManagedServerError(f"{name} is not listed in SHA256SUMS of release {self.version}")

    async def _download(
        self,
        session: aiohttp.ClientSession,
        url: str,
        target: Path,
        progress: Callable[[float], None] | None,
    ) -> str:
        digest = hashlib.sha256()
        try:
            async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as resp:
                resp.raise_for_status()
                total = resp.content_length or 0
                done = 0
                with await self.hass.async_add_executor_job(target.open, "wb") as handle:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        digest.update(chunk)
                        await self.hass.async_add_executor_job(handle.write, chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(min(done / total, 1.0))
        except aiohttp.ClientError as err:
            raise ManagedServerError(f"Download failed: {err}") from err
        return digest.hexdigest()

    def _extract(self, archive: Path) -> None:
        staging = self.install_dir.with_name(self.install_dir.name + ".partial")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")  # data-filter: без абсолютних шляхів/symlink-втеч
        for rel in (EXECUTABLE, "jre/bin/java"):
            path = staging / rel
            if not path.exists():
                raise ManagedServerError(f"Bundle is incomplete: {rel} missing")
            path.chmod(0o755)
        for helper in (staging / "jre" / "lib").glob("jspawnhelper"):
            helper.chmod(0o755)
        (staging / ".installed").write_text(self.version)
        shutil.rmtree(self.install_dir, ignore_errors=True)
        staging.rename(self.install_dir)

    # ---------------------------------------------------------------- run

    async def async_acquire(self) -> None:
        """Start (if needed) and register one user (config entry or flow)."""
        self._users += 1
        if self._idle_stop is not None:
            self._idle_stop.cancel()
            self._idle_stop = None
        if self._supervisor is None:
            self._stopping = False
            self._started.clear()
            self._supervisor = self.hass.async_create_background_task(
                self._supervise(), name=f"{DOMAIN}_managed_server"
            )
        try:
            await asyncio.wait_for(self._started.wait(), 60)
        except asyncio.TimeoutError as err:
            await self.async_release(idle_delay=0)
            raise ManagedServerError("Managed server did not start in 60 s (see logs)") from err

    async def async_release(self, idle_delay: float = IDLE_STOP_DELAY) -> None:
        """Drop one user; stop the process when nobody has needed it for ``idle_delay`` s."""
        self._users = max(0, self._users - 1)
        if self._users > 0:
            return
        if idle_delay <= 0:
            await self.async_stop()
            return
        if self._idle_stop is None:
            self._idle_stop = self.hass.loop.call_later(
                idle_delay, lambda: self.hass.async_create_task(self._stop_if_idle())
            )

    async def _stop_if_idle(self) -> None:
        self._idle_stop = None
        if self._users == 0:
            await self.async_stop()

    async def async_stop(self) -> None:
        if self._idle_stop is not None:
            self._idle_stop.cancel()
            self._idle_stop = None
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        await self._terminate()

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("Server__", "SignalCli__"))}
        env.update(
            {
                "Server__Host": "127.0.0.1",
                "Server__Port": str(self.port),
                "Server__Admin__Enabled": "false",
                "Server__Auth__Enabled": "false",
                "Persistence__DataDirectory": str(self.data_dir),
                "SignalCli__LibDirectory": str(self.install_dir / "signal-cli" / "lib"),
                "SignalCli__StoragePathCli": str(self.data_dir / "SignalCliStorageData"),
                "SignalCli__JavaExecutable": str(self.install_dir / "jre" / "bin" / "java"),
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            }
        )
        return env

    async def _spawn(self) -> asyncio.subprocess.Process:
        if self.port is None:
            # Порт стабільний між перезапусками процесу — клієнт перепідключається за тим самим URL.
            self.port = await self.hass.async_add_executor_job(_free_port)
        await self.hass.async_add_executor_job(lambda: self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True))
        proc = await asyncio.create_subprocess_exec(
            str(self.install_dir / EXECUTABLE),
            cwd=str(self.install_dir),
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        _LOGGER.debug("WsRpcServer запущено (pid %s, порт %s)", proc.pid, self.port)
        return proc

    async def _wait_listening(self, proc: asyncio.subprocess.Process, timeout: float = 45) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline and proc.returncode is None:
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError:
                await asyncio.sleep(0.5)
                continue
            writer.close()
            return True
        return False

    async def _pump_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            _LOGGER.debug("[server] %s", raw.decode("utf-8", "replace").rstrip())

    async def _supervise(self) -> None:
        delay = 2.0
        while not self._stopping:
            try:
                proc = await self._spawn()
            except OSError as err:
                _LOGGER.error("Не вдалося запустити WsRpcServer: %s", err)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RESTART_BACKOFF_MAX)
                continue
            self._proc = proc
            pump = asyncio.create_task(self._pump_logs(proc))
            if await self._wait_listening(proc):
                self._started.set()
                delay = 2.0
            code = await proc.wait()
            pump.cancel()
            self._started.clear()
            if self._stopping:
                break
            _LOGGER.warning("WsRpcServer завершився (код %s), перезапуск через %.0f с", code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESTART_BACKOFF_MAX)

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.warning("WsRpcServer не зупинився за %.0f с — kill", STOP_TIMEOUT)
            proc.kill()
            await proc.wait()


def get_manager(hass: HomeAssistant) -> ManagedServer:
    """Singleton per hass."""
    data = hass.data.setdefault(DOMAIN, {})
    if "manager" not in data:
        manager = data["manager"] = ManagedServer(hass)

        async def _on_stop(_: Event) -> None:
            await manager.async_stop()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    return data["manager"]
