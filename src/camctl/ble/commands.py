"""One command table for direct connections and the daemon: dispatch(remote, cmd, args) -> JSON-safe dict."""

from __future__ import annotations

from typing import Any

from .base import Remote, seconds_arg

# cmd → allowed argument names
COMMANDS: dict[str, tuple[str, ...]] = {
    "status": (),
    "shoot": ("af", "hold", "burst"),
    "record": ("action",),
    "bulb": ("seconds",),
    "autofocus": ("seconds",),
    "zoom": ("direction", "seconds", "speed"),
    "focus": ("direction", "seconds", "speed"),
    "af_on": ("seconds",),
    "c1": (),
}


def check_args(cmd: str, args: dict[str, Any]) -> None:
    if cmd not in COMMANDS:
        raise ValueError(f"Unknown BLE command {cmd!r}; use one of: {', '.join(COMMANDS)}.")
    bad = sorted(set(args) - set(COMMANDS[cmd]))
    if bad:
        allowed = ", ".join(COMMANDS[cmd]) or "none"
        raise ValueError(f"Unknown argument(s) {', '.join(bad)} for {cmd}; allowed: {allowed}.")


def _speed(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"speed must be an integer 1-127, got {v!r}.") from None


async def dispatch(remote: Remote, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    check_args(cmd, args)
    base = {"brand": remote.brand, "address": remote.address}
    if cmd == "status":
        return {**base, "connected": remote.connected, "status": dict(remote.status),
                "capabilities": sorted(remote.capabilities)}
    if cmd == "shoot":
        r = await remote.shoot(af=bool(args.get("af", True)), hold=args.get("hold"), burst=args.get("burst"))
        return r.to_dict()
    if cmd == "record":
        return (await remote.record(str(args.get("action", "toggle")))).to_dict()
    if cmd == "bulb":
        if "seconds" not in args:
            raise ValueError("bulb needs seconds.")
        return (await remote.bulb(args["seconds"])).to_dict()
    if cmd == "autofocus":
        return await remote.autofocus(args.get("seconds", 0.5))
    if cmd in ("zoom", "focus"):
        if "direction" not in args:
            raise ValueError(f"{cmd} needs direction ({'tele/wide' if cmd == 'zoom' else 'in/out'}).")
        fn = remote.zoom if cmd == "zoom" else remote.focus
        await fn(str(args["direction"]), seconds_arg(args.get("seconds", 0.5), cmd, 60), _speed(args.get("speed", 0x20)))
    elif cmd == "af_on":
        await remote.af_on(seconds_arg(args.get("seconds", 0.5), "af_on", 60))
    elif cmd == "c1":
        await remote.c1()
    return {**base, "ok": True, "status": dict(remote.status)}
