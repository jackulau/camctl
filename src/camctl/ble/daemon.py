"""Optional persistent BLE link: a small asyncio server on a unix socket that keeps one camera connected.

Why: connecting + bonding checks cost seconds per command; a held link makes shots near-instant and lets GoPro-style
keep-alives run. CLI/MCP call `send_via_daemon(cmd, args)` first and connect directly when it returns None.

Protocol: newline-delimited JSON over ~/.config/camctl/ble.sock (mode 0600).
  request   {"id": 1, "cmd": "shoot", "args": {"af": true}}
  response  {"id": 1, "ok": true, "result": {...}}  |  {"id": 1, "ok": false, "error": "...", "type": "PairingRequired"}
Commands: every name in commands.COMMANDS, plus "ping" (daemon info) and "shutdown".
Commands are serialised; the connection is (re)established lazily and by a supervisor with exponential backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ..paths import CONFIG_DIR
from .base import Remote
from .commands import dispatch
from .errors import ERRORS, BleError, DaemonError, NotSupported

log = logging.getLogger("camctl.ble")

SOCKET = CONFIG_DIR / "ble.sock"
MAX_LINE = 64 * 1024
_MAX_SOCK_PATH = 100          # macOS sun_path is 104 bytes


class BleDaemon:
    def __init__(self, remote: Remote, socket_path: str | Path | None = None, *, keepalive_interval: float = 5.0,
                 command_timeout: float = 3700.0, max_backoff: float = 30.0, client_idle_timeout: float = 300.0) -> None:
        self.remote = remote
        self.socket_path = Path(socket_path or SOCKET)
        self.keepalive_interval = keepalive_interval
        self.command_timeout = command_timeout
        self.max_backoff = max_backoff
        self.client_idle_timeout = client_idle_timeout
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._server: asyncio.base_events.Server | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    # -- lifecycle ------------------------------------------------------------------------
    async def start(self) -> None:
        path = self.socket_path
        if len(str(path).encode()) > _MAX_SOCK_PATH:
            raise DaemonError(f"Socket path {path} is too long for a unix socket; pass a shorter socket_path.")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            if await _alive(path):
                raise DaemonError(f"A BLE daemon is already running on {path}; use it, or send it 'shutdown' first.")
            path.unlink(missing_ok=True)          # stale socket from a crashed daemon
        self._server = await asyncio.start_unix_server(self._handle, path=str(path), limit=MAX_LINE)
        os.chmod(path, 0o600)
        self._supervisor = asyncio.create_task(self._supervise())
        log.info("BLE daemon for %s listening on %s", self.remote.address, path)

    def request_stop(self) -> None:
        self._stop.set()

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        if self._server is not None:
            self._server.close()
            for w in list(self._writers):
                w.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None
        try:     # let a running command finish its release, but don't hang shutdown on a long bulb
            await asyncio.wait_for(self._lock.acquire(), 5)
            locked = True
        except (asyncio.TimeoutError, TimeoutError):
            locked = False
        try:
            await self.remote.disconnect()
        finally:
            if locked:
                self._lock.release()
        with contextlib.suppress(OSError):
            self.socket_path.unlink()

    # -- connection supervision --------------------------------------------------------------
    async def _supervise(self) -> None:
        backoff = first = min(1.0, self.max_backoff)
        while not self._stop.is_set():
            delay = self.keepalive_interval
            try:
                async with self._lock:
                    if not self.remote.connected:
                        await self.remote.connect()
                        log.info("BLE daemon connected to %s", self.remote.address)
                    elif not await self.remote.keepalive():
                        await self.remote.disconnect()
                        raise BleError("link dropped")
                self.last_error = None
                backoff = first
            except asyncio.CancelledError:
                raise
            except NotSupported as e:
                self.last_error = str(e)
                log.error("BLE daemon cannot serve %s: %s", self.remote.address, e)
                self._stop.set()
                return
            except Exception as e:  # noqa: BLE001 — keep trying; commands report the error to callers
                self.last_error = str(e)
                log.warning("BLE daemon: %s (retrying in %.0fs)", e, backoff)
                delay, backoff = backoff, min(backoff * 2, self.max_backoff)
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self._stop.wait(), delay)

    # -- request handling ----------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while not self._stop.is_set():
                try:
                    line = await asyncio.wait_for(reader.readline(), self.client_idle_timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    break
                except ValueError:        # line longer than MAX_LINE
                    writer.write(_encode({"ok": False, "type": "ValueError",
                                          "error": f"Request larger than {MAX_LINE} bytes."}))
                    await writer.drain()
                    break
                if not line:
                    break
                writer.write(_encode(await self._respond(line)))
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _respond(self, line: bytes) -> dict[str, Any]:
        try:
            req = json.loads(line)
            cmd = req["cmd"]
            args = dict(req.get("args") or {})
            rid = req.get("id")
            if not isinstance(cmd, str):
                raise TypeError
        except (ValueError, KeyError, TypeError, AttributeError):
            return {"ok": False, "type": "ValueError",
                    "error": "Bad request: send one JSON object per line with a 'cmd' string and optional 'args'."}
        want = args.pop("address", None)
        if want and str(want).lower() != self.remote.address.lower():
            return {"id": rid, "ok": False, "type": "WrongDevice",
                    "error": f"This daemon serves {self.remote.address}, not {want}."}
        if cmd == "ping":
            return {"id": rid, "ok": True, "result": {"pong": True, "brand": self.remote.brand,
                                                      "address": self.remote.address, "name": self.remote.name,
                                                      "connected": self.remote.connected, "pid": os.getpid(),
                                                      "last_error": self.last_error}}
        if cmd == "shutdown":
            self._stop.set()
            return {"id": rid, "ok": True, "result": {"stopping": True}}
        try:
            result = await asyncio.wait_for(self._run(cmd, args), self.command_timeout)
            return {"id": rid, "ok": True, "result": result}
        except (asyncio.TimeoutError, TimeoutError):
            return {"id": rid, "ok": False, "type": "DaemonError",
                    "error": f"{cmd} did not finish within {self.command_timeout:g} s; the daemon will reconnect."}
        except Exception as e:  # noqa: BLE001 — reported to the client
            return {"id": rid, "ok": False, "type": type(e).__name__ if type(e).__name__ in ERRORS else "BleError",
                    "error": str(e) or type(e).__name__}

    async def _run(self, cmd: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if not self.remote.connected:
                await self.remote.connect()
            try:
                return await dispatch(self.remote, cmd, args)
            except BleError as e:
                if not isinstance(e, NotSupported) and not self.remote.connected:
                    await self.remote.disconnect()      # reset state so the next command reconnects cleanly
                raise


def _encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, default=str) + "\n").encode()


async def _alive(path: Path) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1.0)
    except (OSError, asyncio.TimeoutError, TimeoutError):
        return False
    w.close()
    with contextlib.suppress(Exception):
        await w.wait_closed()
    return True


async def send_via_daemon(cmd: str, args: dict[str, Any] | None = None, *, address: str | None = None,
                          socket_path: str | Path | None = None, timeout: float = 3700.0) -> dict[str, Any] | None:
    """Run a command on the daemon. None when no daemon is running (or it serves a different camera).

    Errors from the camera are re-raised as the same BleError subclass the direct path would raise.
    """
    path = Path(socket_path or SOCKET)
    if not path.exists():
        return None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=MAX_LINE), 1.0)
    except (OSError, asyncio.TimeoutError, TimeoutError):
        return None        # stale socket file: nobody listening
    payload = dict(args or {})
    if address:
        payload["address"] = address
    try:
        writer.write(_encode({"id": 1, "cmd": cmd, "args": payload}))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        raise DaemonError(f"The BLE daemon did not answer {cmd!r} within {timeout:g} s; restart it.") from None
    except (ConnectionError, OSError):
        return None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if not line:
        raise DaemonError("The BLE daemon closed the connection without answering; check its log or restart it.")
    try:
        resp = json.loads(line)
    except ValueError:
        raise DaemonError("The BLE daemon sent an unreadable reply; restart it.") from None
    if resp.get("ok"):
        return resp.get("result") or {}
    if resp.get("type") == "WrongDevice":
        return None
    raise ERRORS.get(resp.get("type", ""), BleError)(resp.get("error") or "BLE daemon error")


async def daemon_status(socket_path: str | Path | None = None) -> dict[str, Any] | None:
    """Ping result ({pong, brand, address, connected, pid, last_error}) or None if no daemon is running."""
    try:
        return await send_via_daemon("ping", socket_path=socket_path, timeout=5.0)
    except DaemonError:
        return None
