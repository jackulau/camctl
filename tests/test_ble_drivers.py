"""Multi-brand BLE drivers, pairing, bond self-heal, store migration and the daemon. Fakes only: no Bluetooth."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile

import bleak
import pytest
from bleak.exc import BleakDeviceNotFoundError, BleakError, BleakGATTProtocolError

from camctl import ble
from camctl.ble import api, base, bond, canon, daemon, fujifilm, gopro, nikon, panasonic, sony, store

_real_sleep = asyncio.sleep
CB14 = 'Error Domain=CBErrorDomain Code=14 "Peer removed pairing information"'
FF01 = "8000ff01-ff00-ffff-ffff-ffffffffffff"
FF02 = "8000ff02-ff00-ffff-ffff-ffffffffffff"
SONY_LAYOUT = [(sony.REMOTE_SERVICE, [(FF01, ["write"]), (FF02, ["notify"])])]


# -- fakes -----------------------------------------------------------------------------------

class Char:
    def __init__(self, uuid, props):
        self.uuid, self.properties = uuid, list(props)


class Svc:
    def __init__(self, uuid, chars):
        self.uuid, self.characteristics = uuid, chars


class Services:
    def __init__(self, svcs):
        self._svcs = svcs

    def get_service(self, uuid):
        return next((s for s in self._svcs if s.uuid == str(uuid).lower()), None)

    def __iter__(self):
        return iter(self._svcs)


class World:
    """Scenario shared by every fake client a test creates."""

    def __init__(self):
        self.layout: list = []
        self.responder = None          # fn(client, char_uuid, data)
        self.reads: dict = {}
        self.connect_errors: list = []
        self.write_errors: list = []
        self.notify_errors: list = []
        self.clients: list = []
        self.sleeps: list = []
        self.adverts: list = []        # (address, Adv) for the fake scanner

    @property
    def client(self):
        return self.clients[-1]

    def all_writes(self, uuid=None):
        return [(u, d) for c in self.clients for u, d, _ in c.writes if uuid is None or u == uuid]


class FakeClient:
    def __init__(self, world, address, timeout):
        self.world, self.address, self.connected = world, address, False
        self.writes, self.subs, self.cbs = [], [], {}
        self.services = Services([Svc(s, [Char(u, p) for u, p in chars]) for s, chars in world.layout])
        world.clients.append(self)

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        if self.world.connect_errors:
            raise self.world.connect_errors.pop(0)
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def start_notify(self, char, cb):
        if self.world.notify_errors:
            raise self.world.notify_errors.pop(0)
        self.subs.append(char.uuid)
        self.cbs[char.uuid] = (char, cb)

    async def read_gatt_char(self, char):
        return bytearray(self.world.reads[char.uuid])

    async def write_gatt_char(self, char, data, response=False):
        if not self.connected:
            raise BleakError("not connected")
        if self.world.write_errors:
            raise self.world.write_errors.pop(0)
        self.writes.append((char.uuid, bytes(data), response))
        if self.world.responder:
            self.world.responder(self, char.uuid, bytes(data))

    def emit(self, uuid, data):
        if uuid in self.cbs:                          # like the radio: unsubscribed notifications are lost
            char, cb = self.cbs[uuid]
            cb(char, bytearray(data))


class Dev:
    def __init__(self, address, name=None):
        self.address, self.name = address, name


class Adv:
    def __init__(self, name=None, mfg=None, uuids=(), rssi=-50):
        self.local_name, self.manufacturer_data, self.service_uuids, self.rssi = name, mfg or {}, list(uuids), rssi


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path / "ble.json")
    monkeypatch.setattr(daemon, "SOCKET", tmp_path / "no-daemon.sock")


@pytest.fixture
def world(monkeypatch):
    w = World()

    def factory(address, timeout=10.0):
        return FakeClient(w, address, timeout)

    class Scanner:
        def __init__(self, cb):
            self.cb = cb

        async def __aenter__(self):
            for addr, adv in w.adverts:
                self.cb(Dev(addr), adv)
            return self

        async def __aexit__(self, *exc):
            pass

    async def fast_sleep(s=0, *a):
        w.sleeps.append(s)
        await _real_sleep(0)

    monkeypatch.setattr(bleak, "BleakClient", factory)
    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return w


def run(coro):
    return asyncio.run(coro)


async def session(remote, fn):
    async with remote as r:
        return await fn(r)


def sony_responder(on: dict):
    """Emit FF02 notifications after given FF01 writes: {b'\\x01\\x09': [b'\\x02\\xa0\\x20'], ...}."""
    def respond(client, uuid, data):
        if uuid == FF01:
            for note in on.get(data, []):
                client.emit(FF02, note)
    return respond


# -- compatibility -----------------------------------------------------------------------------

def test_backward_compatible_imports():
    from camctl.ble import (CMD, STATE, BleRemote, focus_cmd, parse_status, remember,  # noqa: F401
                            remembered, scan, zoom_cmd)
    assert ble.BleRemote is ble.SonyRemote and issubclass(BleRemote, ble.Remote)
    assert CMD["shutter_down"] == b"\x01\x09" and parse_status(b"\x02\xa0\x20") == ("shutter", True)
    assert zoom_cmd("tele", 0x20, True) == b"\x02\x45\x30" and focus_cmd("out", 1, False) == b"\x02\x6c\x00"
    assert ble.STATE.name == "ble.json" and ble.REMOTE_SERVICE == sony.REMOTE_SERVICE
    f = ble.Found("AA", "ILCE-7M5", -40)          # positional order of the Sony-only API
    assert f.rssi == -40 and f.brand == "sony" and f.supported
    assert callable(ble.scan) and callable(ble.remembered) and callable(ble.remember)
    assert set(ble.supported_brands()) == {"sony", "canon", "fujifilm", "nikon", "gopro", "panasonic", "omsystem"}


def test_errors_are_runtime_errors():
    for cls in (ble.NotSupported, ble.PairingNotSaved, ble.BondRestartRequired, ble.DaemonError):
        assert issubclass(cls, RuntimeError)


# -- Sony ------------------------------------------------------------------------------------------

def test_sony_pair_retries_insufficient_encryption_then_asks_for_ok(world):
    world.layout = SONY_LAYOUT
    world.write_errors = [BleakGATTProtocolError(15)] * 3        # macOS still bonding
    prompts = []
    r = ble.SonyRemote("AA", name="ILCE-7M5", on_prompt=prompts.append, confirm_wait=0)

    async def go():
        try:
            return await r.pair()
        finally:
            await r.disconnect()
    res = run(go())
    assert world.client.subs == [FF02]                           # subscribing does not bond; the write does
    assert world.client.writes == [(FF01, b"\x01\x06", True)]    # harmless probe, write-with-response
    assert world.sleeps.count(3.0) == 3                          # retried every 3 s
    assert sum(p.startswith("Bonding with the camera") for p in prompts) == 1 and any("Press OK on the camera now" in p for p in prompts)
    assert res.bonded is True and res.brand == "sony"


def test_sony_gives_up_with_pairing_guidance(world):
    world.layout = SONY_LAYOUT
    world.write_errors = [BleakGATTProtocolError(15)] * 10
    r = ble.SonyRemote("AA", bond_timeout=6, bond_retry=3)
    with pytest.raises(ble.PairingRequired) as ei:
        run(session(r, lambda r: r.send(ble.CMD["focus_up"])))
    assert "Pairing" in str(ei.value) and world.client.writes == []


def test_sony_resubscribes_after_bonding_so_feedback_works(world):
    world.layout = SONY_LAYOUT
    world.notify_errors = [BleakGATTProtocolError(15)]           # FF02 subscription refused before encryption
    world.responder = sony_responder({b"\x01\x07": [b"\x02\x3f\x20"], b"\x01\x09": [b"\x02\xa0\x20"]})
    res = run(session(ble.SonyRemote("AA"), lambda r: r.shoot()))
    assert world.client.subs == [FF02] and res.feedback is True and res.released is True


def test_sony_peer_removed_pairing_is_actionable(world):
    world.layout = SONY_LAYOUT
    world.connect_errors = [BleakError(CB14)]
    with pytest.raises(ble.PairingNotSaved) as ei:
        run(session(ble.SonyRemote("AA", name="ILCE-7M5"), lambda r: r.shoot()))
    msg = str(ei.value)
    assert "ILCE-7M5" in msg and "Forget" in msg and "press OK" in msg


def test_verify_pairing_detects_discarded_bond(world):
    world.layout = SONY_LAYOUT
    r = ble.SonyRemote("AA", name="ILCE-7M5")

    async def go():
        await r.connect()
        world.connect_errors = [BleakError(CB14)]
        try:
            await r.verify_pairing(settle=0)
        finally:
            await r.disconnect()
    with pytest.raises(ble.PairingNotSaved) as ei:
        run(go())
    assert "press OK" in str(ei.value) and "did not keep" in str(ei.value)


def test_verify_pairing_ok_reconnects_and_probes(world):
    world.layout = SONY_LAYOUT
    r = ble.SonyRemote("AA")

    async def go():
        await r.connect()
        ok = await r.verify_pairing(settle=0)
        await r.disconnect()
        return ok
    assert run(go()) is True and len(world.clients) == 2 and world.clients[1].writes[0][1] == b"\x01\x06"


def test_sony_shot_records_notification_sequence(world):
    world.layout = SONY_LAYOUT
    world.responder = sony_responder({b"\x01\x07": [b"\x02\x3f\x20"], b"\x01\x09": [b"\x02\xa0\x20"],
                                      b"\x01\x08": [b"\x02\xa0\x00"]})
    res = run(session(ble.SonyRemote("AA"), lambda r: r.shoot()))
    assert [w for _, w, _ in world.client.writes] == [b"\x01\x07", b"\x01\x09", b"\x01\x08", b"\x01\x06"]
    assert [(e["event"], e["value"]) for e in res.events] == [("focus", True), ("shutter", True), ("shutter", False)]
    assert res.events[1]["raw"] == "02a020" and all(e["t"] >= 0 for e in res.events)
    assert res.released is True and res.shutter_active and res.focus_locked is True and res.hint is None
    assert res.hold == 0.03 and 0.03 in world.sleeps               # minimal hold: one frame in Continuous drive
    assert res.status["shutter"] is False                          # last state alone would look like a miss


def test_sony_shot_counts_any_shutter_notification(world):
    world.layout = SONY_LAYOUT
    world.responder = sony_responder({b"\x01\x08": [b"\x02\xa0\x00"]})
    res = run(session(ble.SonyRemote("AA"), lambda r: r.shoot(af=False)))
    assert res.released is True and res.shutter_active is False


def test_sony_shot_without_release_explains_focus(world):
    world.layout = SONY_LAYOUT                                     # camera silent: AF could not lock
    res = run(session(ble.SonyRemote("AA"), lambda r: r.shoot()))
    assert [w for _, w, _ in world.client.writes] == [b"\x01\x07", b"\x01\x09", b"\x01\x08", b"\x01\x06"]
    assert res.released is False and res.focus_locked is False
    assert "lens cap" in res.hint and "MF/DMF" in res.hint and "af=False" in res.hint
    d = res.to_dict()
    assert json.loads(json.dumps(d)) == d


def test_sony_shot_focus_but_no_shutter_hint(world):
    world.layout = SONY_LAYOUT
    world.responder = sony_responder({b"\x01\x07": [b"\x02\x3f\x20"]})
    res = run(session(ble.SonyRemote("AA"), lambda r: r.shoot()))
    assert res.released is False and res.focus_locked is True and "no shutter activity" in res.hint


def test_sony_burst_holds_release(world):
    world.layout = SONY_LAYOUT
    res = run(session(ble.SonyRemote("AA"), lambda r: r.shoot(burst=0.8)))
    assert res.hold == 0.8 and 0.8 in world.sleeps
    with pytest.raises(ValueError):
        run(session(ble.SonyRemote("AA"), lambda r: r.shoot(burst=-1)))


def test_sony_record_without_notification_explains(world):
    world.layout = SONY_LAYOUT
    res = run(session(ble.SonyRemote("AA"), lambda r: r.record()))
    assert [w for _, w, _ in world.client.writes] == [b"\x01\x0f", b"\x01\x0e"]
    assert res.recording is None and res.reported is False and "Photo/Movie switch on Movie" in res.hint


def test_sony_record_start_corrects_unknown_state(world):
    world.layout = SONY_LAYOUT
    notes = iter([b"\x02\xd5\x00", b"\x02\xd5\x20"])               # it was recording: first toggle stopped it

    def respond(client, uuid, data):
        if data == b"\x01\x0f":
            client.emit(FF02, next(notes))
    world.responder = respond
    res = run(session(ble.SonyRemote("AA"), lambda r: r.record("start")))
    assert [w for _, w, _ in world.client.writes] == [b"\x01\x0f", b"\x01\x0e"] * 2
    assert res.recording is True and res.reported and res.hint is None


def test_sony_record_stop_is_noop_when_known_stopped(world):
    world.layout = SONY_LAYOUT
    world.responder = sony_responder({b"\x01\x0f": [b"\x02\xd5\x00"]})

    async def go(r):
        first = await r.record("toggle")
        n = len(world.client.writes)
        second = await r.record("stop")
        return first, second, len(world.client.writes) - n
    first, second, extra_writes = run(session(ble.SonyRemote("AA"), go))
    assert first.recording is False and second.changed is False and extra_writes == 0
    with pytest.raises(ValueError):
        run(session(ble.SonyRemote("AA"), lambda r: r.record("pause")))


def test_sony_bad_directions_are_value_errors(world):
    world.layout = SONY_LAYOUT
    with pytest.raises(ValueError):
        run(session(ble.SonyRemote("AA"), lambda r: r.zoom("up", 0.1)))
    with pytest.raises(ValueError):
        run(session(ble.SonyRemote("AA"), lambda r: r.focus("near", 0.1)))


def test_commands_need_connection():
    with pytest.raises(ble.BleError, match="Not connected"):
        run(ble.SonyRemote("AA").shoot())


# -- Canon -----------------------------------------------------------------------------------------

CANON_LAYOUT = [(canon.SERVICE, [(canon.PAIR_CHAR, ["write"]),
                                 (canon.CONTROL_CHAR, ["write", "write-without-response"])])]


def test_canon_pairing_and_control_bytes(world):
    world.layout = CANON_LAYOUT
    world.write_errors = [BleakGATTProtocolError(15)]              # bonding on the first encrypted write
    prompts = []
    r = ble.CanonRemote("CA", on_prompt=prompts.append, confirm_wait=0)

    async def go():
        await r.pair()
        await r.shoot()
        await r.shoot(af=False)
        rec = await r.record()
        await r.zoom("tele", 0.1)
        await r.disconnect()
        return rec
    rec = run(go())
    w = world.client.writes
    assert w[0] == (canon.PAIR_CHAR, b"\x03camctl", True)
    ctrl = [(d[0], resp) for u, d, resp in w if u == canon.CONTROL_CHAR]
    assert all(resp is False for _, resp in ctrl)                  # control char: write without response
    assert [c for c, _ in ctrl] == [0x0C, 0x4C, 0x8C, 0x0C, 0x8C, 0x0C, 0x88, 0x08, 0x2C, 0x0C]
    assert any("Paired with: camctl" in p for p in prompts)
    assert rec.recording is None and "does not report" in rec.hint


def test_canon_shot_has_no_feedback(world):
    world.layout = CANON_LAYOUT
    res = run(session(ble.CanonRemote("CA"), lambda r: r.shoot()))
    assert res.released is None and res.feedback is False and res.hint is None


# -- Fujifilm --------------------------------------------------------------------------------------

def _fuji_layout():
    conf = [(u if len(u) > 8 else u + "-1111-2222-3333-444455556666", ["notify"]) for u in fujifilm.CONF_CHARS]
    geo = [(u if len(u) > 8 else u + "-aaaa-bbbb-cccc-ddddeeeeffff", ["notify", "write"]) for u in fujifilm.GEO_CHARS]
    return [(fujifilm.SECURE_PAIR_SERVICE, [(fujifilm.SECURE_PAIR_CHAR, ["read", "write"]),
                                            (fujifilm.IDENTIFIER_CHAR, ["write"])]),
            (fujifilm.BASIC_PAIR_SERVICE, [(fujifilm.BASIC_PAIR_CHAR, ["write"])]),
            (fujifilm.CONF_SERVICE, conf), (fujifilm.GEO_SERVICE, geo),
            (fujifilm.SHUTTER_SERVICE, [(fujifilm.SHUTTER_CHAR, ["write"])])]


def test_fujifilm_secure_handshake_and_shutter(world):
    world.layout = _fuji_layout()
    world.reads[fujifilm.SECURE_PAIR_CHAR] = b"\x11\x22\x33\x44"
    r = ble.FujifilmRemote("FJ", extra={"secure": True})
    run(session(r, lambda r: r.shoot()))
    c = world.client
    assert [(u, d) for u, d, _ in c.writes[:3]] == [
        (fujifilm.SECURE_PAIR_CHAR, b"\x11\x22\x33\x20"), (fujifilm.IDENTIFIER_CHAR, b"camctl"),
        (fujifilm.GEO_INTERVAL_CHAR, b"\x0a\x00")]
    assert len(c.subs) == 12 and "e6692c5c-1111-2222-3333-444455556666" in c.subs    # prefix match
    assert [d for u, d, _ in c.writes[3:]] == [b"\x01\x00", b"\x03\x00", b"\x01\x00", b"\x02\x00",
                                              b"\x01\x00", b"\x00\x00"]
    assert all(u == fujifilm.SHUTTER_CHAR for u, _, _ in c.writes[3:])


def test_fujifilm_basic_token_and_no_record(world):
    world.layout = _fuji_layout()
    r = ble.FujifilmRemote("FJ", extra={"secure": False, "token": "a1b2c3d4"})

    async def go(r):
        with pytest.raises(ble.NotSupported, match="movie recording"):
            await r.record()
    run(session(r, go))
    assert [(u, d) for u, d, _ in world.client.writes] == [(fujifilm.BASIC_PAIR_CHAR, bytes.fromhex("a1b2c3d4")),
                                                          (fujifilm.IDENTIFIER_CHAR, b"camctl")]
    with pytest.raises(ble.PairingRequired):
        run(session(ble.FujifilmRemote("FJ", extra={"secure": False, "token": ""}), lambda r: r.shoot()))


# -- Nikon -----------------------------------------------------------------------------------------

NIKON_LAYOUT = [(nikon.SERVICE, [(nikon.SHUTTER_CHAR, ["write"]), (nikon.INDICATE_CHAR, ["indicate"]),
                                 (nikon.PAIR_CHAR, ["write", "indicate"])])]


def nikon_responder(client, uuid, data):
    if uuid == nikon.PAIR_CHAR and data[0] == 0x01:
        client.emit(nikon.PAIR_CHAR, b"\x02" + b"\x00" * 16)
    elif uuid == nikon.PAIR_CHAR and data[0] == 0x03:
        client.emit(nikon.PAIR_CHAR, b"\x04" + b"\x00" * 8 + b"SN000042")


def test_nikon_handshake_bytes_and_persisted_dev_id(world):
    world.layout, world.responder = NIKON_LAYOUT, nikon_responder
    r = ble.NikonRemote("NK")

    async def go():
        res = await r.pair()
        await r.verify_pairing(settle=0)
        await r.shoot()
        await r.disconnect()
        return res
    res = run(go())
    first, second = world.clients[0], world.clients[1]
    assert first.subs == [nikon.INDICATE_CHAR, nikon.PAIR_CHAR]
    hello = first.writes[0][1]
    assert len(hello) == 17 and hello[0] == 1 and hello[1:8] == b"\x00" * 7 and hello[8] == 1 and hello[9] == 1
    assert first.writes[1][1] == b"\x03" + b"\x00" * 16
    assert res.extra["dev_id"] == hello[9:13].hex() and res.extra["serial"] == "SN000042" and res.bonded is False
    assert second.writes[0][1][9:13] == hello[9:13]              # reconnect reuses the dev id
    assert [d for u, d, _ in second.writes if u == nikon.SHUTTER_CHAR] == [b"\x02\x02", b"\x02\x00"]


def test_nikon_handshake_timeout_and_unpaired(world, monkeypatch):
    world.layout = NIKON_LAYOUT
    monkeypatch.setattr(nikon.NikonRemote, "stage_timeout", 0.01)
    with pytest.raises(ble.PairingFailed, match="stage 1"):
        run(ble.NikonRemote("NK").pair())
    with pytest.raises(ble.PairingRequired):
        run(session(ble.NikonRemote("NK"), lambda r: r.shoot()))
    with pytest.raises(ble.NotSupported, match="autofocus"):
        run(ble.NikonRemote("NK").autofocus())


# -- Panasonic -------------------------------------------------------------------------------------

PANA_LAYOUT = [(panasonic.PAIR_SERVICE, [(panasonic.MEI_CHAR, ["write"]), (panasonic.NAME_CHAR, ["write"])]),
               (panasonic.REMOTE_SERVICE, [(panasonic.REMOTE_CTRL, ["write"]), (panasonic.REMOTE_NOTIFY, ["notify"])])]


def test_panasonic_remote_mode_and_buttons(world):
    world.layout = PANA_LAYOUT
    world.responder = lambda c, u, d: c.emit(panasonic.REMOTE_NOTIFY, b"\x00") if d == b"\x01" else None

    async def go(r):
        await r.shoot()
        await r.record()
    run(session(ble.PanasonicRemote("LX"), go))
    assert [d for _, d, _ in world.client.writes] == [
        bytes.fromhex("4d454930010010008001020000030b54"), b"camctl", b"\x01", b"\x02",
        b"\x04", b"\x05", b"\x06", b"\x07"]


def test_panasonic_error_code(world):
    world.layout = PANA_LAYOUT
    world.responder = lambda c, u, d: c.emit(panasonic.REMOTE_NOTIFY, b"\x02") if d == b"\x01" else None
    with pytest.raises(ble.ConnectFailed, match="switched off"):
        run(session(ble.PanasonicRemote("LX"), lambda r: r.shoot()))


# -- GoPro -----------------------------------------------------------------------------------------

GOPRO_LAYOUT = [(gopro.SERVICE, [(gopro.COMMAND_CHAR, ["write"]), (gopro.RESPONSE_CHAR, ["notify"]),
                                 (gopro.SETTINGS_CHAR, ["write"])])]


def gopro_responder(status=0):
    def respond(client, uuid, data):
        if uuid == gopro.COMMAND_CHAR:
            client.emit(gopro.RESPONSE_CHAR, bytes([0x02, data[1], status]))
    return respond


def test_gopro_shutter_record_and_keepalive(world):
    world.layout, world.responder = GOPRO_LAYOUT, gopro_responder()

    async def go(r):
        shot = await r.shoot()
        start = await r.record("start")
        stop = await r.record("toggle")
        await r.keepalive()
        return shot, start, stop
    shot, start, stop = run(session(ble.GoProRemote("GP"), go))
    assert [d for u, d, _ in world.client.writes if u == gopro.COMMAND_CHAR] == [
        b"\x03\x01\x01\x01", b"\x03\x01\x01\x01", b"\x03\x01\x01\x00"]
    assert b"\x03\x5b\x01\x42" in [d for u, d, _ in world.client.writes if u == gopro.SETTINGS_CHAR]
    assert shot.released is True and start.recording is True and stop.recording is False and stop.reported


def test_gopro_rejected_shutter(world):
    world.layout, world.responder = GOPRO_LAYOUT, gopro_responder(status=2)
    res = run(session(ble.GoProRemote("GP"), lambda r: r.shoot()))
    assert res.released is False and "rejected" in res.hint


# -- unsupported -----------------------------------------------------------------------------------

def test_om_system_is_recognised_but_not_supported(world):
    with pytest.raises(ble.NotSupported, match="no public Bluetooth remote protocol"):
        run(ble.OMSystemRemote("OM").connect())
    with pytest.raises(ble.NotSupported, match="USB"):
        run(ble.OMSystemRemote("OM").shoot())
    assert world.clients == []
    with pytest.raises(ble.NotSupported, match="Unknown camera brand"):
        ble.driver_for("leica")
    assert ble.driver_for("Fuji") is ble.FujifilmRemote


# -- scan ------------------------------------------------------------------------------------------

def _all_brand_adverts():
    return [
        ("SONY", Adv("ILCE-7M5", {0x012D: bytes.fromhex("03006500553122ffc0")}, rssi=-40)),
        ("HEADPHONES", Adv("WH-1000XM5", {0x012D: bytes.fromhex("040022ef0102")}, rssi=-20)),
        ("CANON", Adv("EOS R6m2", {}, [canon.SERVICE], rssi=-45)),
        ("FUJIB", Adv("X-T5", {0x04D8: bytes.fromhex("02a1b2c3d4")}, rssi=-50)),
        ("FUJIS", Adv("X-H2S", {0x04D8: bytes.fromhex("0301020304")}, [fujifilm.SECURE_ADV_SERVICE], rssi=-51)),
        ("NIKONP", Adv("Z6_3", {}, [nikon.SERVICE], rssi=-55)),
        ("NIKONR", Adv(None, {0x0399: bytes.fromhex("01aabbcc00")}, rssi=-56)),
        ("LUMIX", Adv("S5-A1B2C3", {}, rssi=-60)),
        ("GOPRO", Adv("GoPro 1234", {}, [gopro.SERVICE], rssi=-65)),
        ("OM", Adv("OM-1", {}, rssi=-70)),
        ("PHONE", Adv("iPhone", {0x004C: b"\x10\x05"}, rssi=-10)),
    ]


def test_scan_detects_every_brand(world):
    world.adverts = _all_brand_adverts()
    found = {f.address: f for f in run(ble.scan(0))}
    assert {a: f.brand for a, f in found.items()} == {
        "SONY": "sony", "CANON": "canon", "FUJIB": "fujifilm", "FUJIS": "fujifilm", "NIKONP": "nikon",
        "NIKONR": "nikon", "LUMIX": "panasonic", "GOPRO": "gopro", "OM": "omsystem"}
    assert found["SONY"].pairing_mode is True and found["SONY"].raw == "03006500553122ffc0"
    assert found["CANON"].pairing_mode is True and found["NIKONP"].pairing_mode is True
    assert found["FUJIB"].extra == {"secure": False, "token": "a1b2c3d4"} and found["FUJIS"].extra["secure"] is True
    assert found["NIKONR"].extra == {"dev_id": "01aabbcc"} and found["NIKONR"].pairing_mode is False
    assert found["NIKONR"].name == "Nikon" and not found["OM"].supported
    assert json.loads(json.dumps(found["FUJIB"].to_dict()))["supported"] is True
    assert list(found) == sorted(found, key=lambda a: -found[a].rssi)


def test_scan_filters(world):
    world.adverts = _all_brand_adverts()
    assert [f.address for f in run(ble.scan(0, brands=["nikon"]))] == ["NIKONP", "NIKONR"]
    assert "OM" not in {f.address for f in run(ble.scan(0, include_unsupported=False))}


# -- remembered devices ----------------------------------------------------------------------------

def test_ble_json_migrates_single_sony_device(tmp_path):
    store.STATE.write_text(json.dumps({"address": "AA", "name": "ILCE-7M5"}))
    assert ble.remembered() == "AA"
    assert [(d.address, d.brand, d.name) for d in ble.devices()] == [("AA", "sony", "ILCE-7M5")]
    ble.remember("BB", "EOS R6", brand="canon", extra={"k": 1})
    data = json.loads(store.STATE.read_text())
    assert data["version"] == 2 and data["default"] == "BB" and set(data["devices"]) == {"AA", "BB"}
    assert ble.remembered() == "BB"
    ble.remember("BB", "EOS R6")                  # legacy 2-argument call keeps brand and extra
    assert ble.get_device("eos r6").brand == "canon" and ble.get_device("BB").extra == {"k": 1}
    assert ble.forget("BB") and ble.remembered() == "AA" and not ble.forget("ZZ")
    assert oct(store.STATE.stat().st_mode & 0o777) == "0o600"


def test_ble_json_corrupt_or_missing(tmp_path):
    assert ble.remembered() is None and ble.devices() == []
    store.STATE.write_text("{not json")
    assert ble.remembered() is None
    ble.remember("AA", "ILCE-7M5")
    assert ble.remembered() == "AA"


def test_patched_package_state_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", store._DEFAULT_STATE)
    p = tmp_path / "other.json"
    monkeypatch.setattr(ble, "STATE", p)
    ble.remember("CC", "X")
    assert json.loads(p.read_text())["default"] == "CC"


# -- bond ------------------------------------------------------------------------------------------

PAIRED = ('address: 12-34-56-78-9a-bc, not connected, not favourite, paired, name: "ILCE-7M5", recent access date: '
          '2026-09-26 10:00:00 +0000\n'
          'address: aa-bb-cc-dd-ee-ff, connected (master, -50 dBm), not favourite, paired, name: "AirPods Pro", '
          'recent access date: 2026-09-26 10:00:00 +0000\n')


def test_error_classification():
    assert bond.is_insufficient_encryption(BleakGATTProtocolError(15))
    assert bond.is_insufficient_encryption(BleakGATTProtocolError(5))
    assert not bond.is_insufficient_encryption(BleakGATTProtocolError(3))
    assert bond.is_insufficient_encryption(BleakError("Error Domain=CBATTErrorDomain Code=15 \"Encryption is "
                                                      "insufficient.\""))
    assert bond.is_peer_removed(BleakError(CB14)) and not bond.is_peer_removed(BleakError("Code=7"))
    assert isinstance(bond.translate_error(BleakDeviceNotFoundError("AA")), ble.CameraNotFound)
    assert isinstance(bond.translate_error(TimeoutError()), ble.ConnectFailed)
    assert isinstance(bond.translate_error(BleakError("Bluetooth device is turned off")), ble.BluetoothUnavailable)


def test_find_paired_mac():
    assert bond.find_paired_mac("ilce-7m5", PAIRED) == "12-34-56-78-9a-bc"
    assert bond.find_paired_mac("12:34:56:78:9A:BC", PAIRED) == "12-34-56-78-9a-bc"
    assert bond.find_paired_mac("ILCE-7M4", PAIRED) is None
    assert bond.parse_paired(PAIRED)[1]["connected"] is True


class Runner:
    def __init__(self, paired_after="", fail_power_off=False):
        self.calls, self.paired_after, self.fail_power_off, self.unpaired = [], paired_after, fail_power_off, False

    async def __call__(self, argv):
        self.calls.append(argv[1:])
        if argv[1:] == ["--paired"]:
            return 0, (self.paired_after if self.unpaired else PAIRED), ""
        if argv[1:] == ["--power", "0"] and self.fail_power_off:
            raise OSError("blueutil crashed")
        if argv[1] == "--unpair":
            self.unpaired = True
        return 0, "", ""


def test_clear_stale_bond_requires_permission():
    r = Runner()
    with pytest.raises(ble.BondRestartRequired) as ei:
        run(bond.clear_stale_bond("ILCE-7M5", runner=r, platform="darwin", blueutil="/opt/homebrew/bin/blueutil"))
    exe = "/opt/homebrew/bin/blueutil"
    assert (f"{exe} --power 0; sleep 3; {exe} --power 1; sleep 5; {exe} --unpair 12-34-56-78-9a-bc"
            in str(ei.value))
    assert "Forget This Device" in str(ei.value) and r.calls == [["--paired"]]


def test_clear_stale_bond_power_cycles_then_unpairs(world):
    r = Runner()
    res = run(bond.clear_stale_bond("ILCE-7M5", True, runner=r, platform="darwin", blueutil="bu"))
    assert r.calls == [["--paired"], ["--power", "0"], ["--power", "1"], ["--unpair", "12-34-56-78-9a-bc"],
                       ["--paired"]]
    assert world.sleeps[:2] == [3, 5] and res == {"cleared": True, "mac": "12-34-56-78-9a-bc", "name": "ILCE-7M5"}


def test_clear_stale_bond_always_restores_power(world):
    r = Runner(fail_power_off=True)
    with pytest.raises(OSError):
        run(bond.clear_stale_bond("ILCE-7M5", True, runner=r, platform="darwin", blueutil="bu"))
    assert ["--power", "1"] in r.calls


def test_clear_stale_bond_edge_cases(world, monkeypatch):
    with pytest.raises(ble.NotSupported):
        run(bond.clear_stale_bond("X", True, platform="linux"))
    monkeypatch.setattr(bond, "find_blueutil", lambda: None)
    with pytest.raises(ble.BondRestartRequired, match="System Settings"):
        run(bond.clear_stale_bond("ILCE-7M5", True, platform="darwin"))
    res = run(bond.clear_stale_bond("ILCE-7M4", True, runner=Runner(), platform="darwin", blueutil="bu"))
    assert res["cleared"] is False
    r = Runner(paired_after=PAIRED)                          # unpair silently failed
    with pytest.raises(ble.BleError, match="Forget This Device"):
        run(bond.clear_stale_bond("ILCE-7M5", True, runner=r, platform="darwin", blueutil="bu"))


# -- api -------------------------------------------------------------------------------------------

def test_execute_direct_uses_remembered_device(world):
    world.layout = SONY_LAYOUT
    world.responder = sony_responder({b"\x01\x09": [b"\x02\xa0\x20"]})
    ble.remember("AA", "ILCE-7M5", "sony")
    res = run(ble.execute("shoot", {"af": False}))
    assert res["via"] == "direct" and res["released"] is True and res["address"] == "AA"
    assert not world.client.connected
    with pytest.raises(ValueError, match="Unknown BLE command"):
        run(ble.execute("explode"))
    assert len(world.clients) == 1                          # validated before connecting


def test_execute_finds_nikon_after_address_change(world):
    world.layout, world.responder = NIKON_LAYOUT, nikon_responder
    ble.remember("OLD", "Z6_3", "nikon", extra={"dev_id": "01aabbcc"})
    world.connect_errors = [BleakDeviceNotFoundError("OLD")]
    world.adverts = [("NEW", Adv(None, {0x0399: bytes.fromhex("01aabbcc00")}))]
    res = run(ble.execute("shoot"))
    assert res["address"] == "NEW" and world.client.address == "NEW"
    assert ble.get_device("NEW").extra["dev_id"] == "01aabbcc" and ble.get_device("NEW").extra["serial"] == "SN000042"


def test_resolve_device_scans_when_nothing_remembered(world):
    world.adverts = _all_brand_adverts()
    dev = run(ble.resolve_device(brand="canon"))
    assert dev.brand == "canon" and ble.remembered() == "CANON"
    world.adverts = []
    store.STATE.unlink()
    with pytest.raises(ble.CameraNotFound, match="No supported camera"):
        run(ble.resolve_device())


def test_pair_heals_stale_bond_only_when_allowed(world, monkeypatch):
    world.layout = SONY_LAYOUT
    world.adverts = [("AA", Adv("ILCE-7M5", {0x012D: bytes.fromhex("03006500553122ffc0")}))]
    calls = []

    async def fake_clear(name, allow, **kw):
        calls.append((name, allow))
        if not allow:
            raise ble.BondRestartRequired(bond.stale_bond_guidance(name, "12-34-56-78-9a-bc"))
        return {"cleared": True, "mac": "12-34-56-78-9a-bc", "name": name}
    monkeypatch.setattr(bond, "clear_stale_bond", fake_clear)

    world.connect_errors = [BleakError(CB14)]
    with pytest.raises(ble.BondRestartRequired, match="blueutil --power 0"):
        run(ble.pair(confirm_wait=0))

    world.connect_errors = [BleakError(CB14)]
    res = run(ble.pair(confirm_wait=0, allow_bt_restart=True))
    assert calls == [("ILCE-7M5", False), ("ILCE-7M5", True)]
    assert res["verified"] is True and res["stale_bond_cleared"]["cleared"] is True
    dev = ble.get_device("AA")
    assert dev.brand == "sony" and dev.extra["mac"] == "12-34-56-78-9a-bc" and ble.remembered() == "AA"


def test_pair_prefers_camera_in_pairing_mode_and_rejects_om(world):
    world.layout = CANON_LAYOUT
    world.adverts = [("SONY", Adv("ILCE-7M5", {0x012D: bytes.fromhex("03006500553122bf00")}, rssi=-30)),
                     ("CANON", Adv("EOS RP", {}, [canon.SERVICE], rssi=-60))]
    res = run(ble.pair(confirm_wait=0, verify=False))
    assert res["brand"] == "canon" and res["address"] == "CANON"
    world.adverts = [("OM", Adv("OM-5", {}))]
    with pytest.raises(ble.NotSupported, match="OM System"):
        run(ble.pair())


def test_capability_table():
    t = ble.supported_brands()
    assert t["sony"]["verified"] == ["shoot", "record"] and t["sony"]["feedback"]["shutter"] is True
    assert t["panasonic"]["experimental"] == ["shoot", "record"] and "record" in t["nikon"]["experimental"]
    assert t["omsystem"]["supported"] is False and t["canon"]["capabilities"] == ["shoot", "autofocus", "record",
                                                                                   "zoom"]


# -- daemon ----------------------------------------------------------------------------------------

class FakeRemote(ble.Remote):
    brand = "sony"
    label = "Sony camera"
    capabilities = frozenset({ble.SHOOT})
    reports_shutter = True

    def __init__(self, address):
        super().__init__(address)
        self.connects = 0

    async def connect(self):
        self.connects += 1
        self._client = object()

    async def disconnect(self):
        self._client = None

    async def _shutter(self, af, hold):
        self._event("shutter", True, b"\x02\xa0\x20")
        self._event("shutter", False, b"\x02\xa0\x00")


@pytest.fixture
def sock():
    d = tempfile.mkdtemp(prefix="cc")                # unix socket paths must stay short
    yield os.path.join(d, "b.sock")
    shutil.rmtree(d, ignore_errors=True)


def test_daemon_round_trip(sock):
    async def go():
        remote = FakeRemote("AA")
        d = ble.BleDaemon(remote, sock, keepalive_interval=0.05)
        await d.start()
        try:
            assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
            r = await ble.send_via_daemon("shoot", {"af": False}, socket_path=sock)
            assert r["released"] is True and r["brand"] == "sony" and r["events"][0]["raw"] == "02a020"
            p = await ble.daemon_status(sock)
            assert p["connected"] is True and p["address"] == "AA"
            assert await ble.send_via_daemon("shoot", socket_path=sock, address="OTHER") is None
            assert (await ble.send_via_daemon("shoot", socket_path=sock, address="aa"))["released"] is True
            with pytest.raises(ble.NotSupported, match="zoom"):
                await ble.send_via_daemon("zoom", {"direction": "tele"}, socket_path=sock)
            with pytest.raises(ValueError, match="bogus"):
                await ble.send_via_daemon("shoot", {"bogus": 1}, socket_path=sock)
            remote._client = None                    # link dropped
            await ble.send_via_daemon("shoot", socket_path=sock)
            assert remote.connects >= 2
            reader, writer = await asyncio.open_unix_connection(sock)
            writer.write(b"not json\n")
            await writer.drain()
            assert json.loads(await reader.readline())["ok"] is False
            writer.close()
            with pytest.raises(ble.DaemonError, match="already running"):
                await ble.BleDaemon(FakeRemote("AA"), sock).start()
        finally:
            await d.stop()
        assert not os.path.exists(sock)
        assert await ble.send_via_daemon("shoot", socket_path=sock) is None
    run(go())


def test_daemon_cleans_stale_socket_and_shuts_down_on_request(sock):
    s = socket.socket(socket.AF_UNIX)
    s.bind(sock)                                     # file exists, nobody listening
    s.close()

    async def go():
        assert await ble.send_via_daemon("shoot", socket_path=sock) is None
        d = ble.BleDaemon(FakeRemote("AA"), sock, keepalive_interval=0.05)
        task = asyncio.create_task(d.serve_forever())
        for _ in range(100):
            if await ble.daemon_status(sock):
                break
            await asyncio.sleep(0.01)
        res = await ble.execute("shoot", socket_path=sock)
        assert res["via"] == "daemon" and res["released"] is True
        assert (await ble.send_via_daemon("shutdown", socket_path=sock))["stopping"] is True
        await asyncio.wait_for(task, 5)
    run(go())
    assert not os.path.exists(sock)


def test_daemon_supervisor_reconnects_with_backoff(sock):
    class Flaky(FakeRemote):
        async def connect(self):
            self.connects += 1
            if self.connects < 3:
                raise ble.ConnectFailed("camera asleep")
            self._client = object()

    async def go():
        remote = Flaky("AA")
        d = ble.BleDaemon(remote, sock, keepalive_interval=0.01, max_backoff=0.01)
        await d.start()
        try:
            for _ in range(300):
                if remote.connected:
                    break
                await asyncio.sleep(0.01)
            assert remote.connected and remote.connects == 3 and d.last_error is None
        finally:
            await d.stop()
    run(go())


def test_socket_path_too_long():
    async def go():
        with pytest.raises(ble.DaemonError, match="too long"):
            await ble.BleDaemon(FakeRemote("AA"), "/tmp/" + "x" * 200).start()
    run(go())


def test_daemon_sources_do_not_print():
    import inspect
    for mod in (base, bond, canon, daemon, fujifilm, gopro, nikon, panasonic, sony, store, api):
        assert "print(" not in inspect.getsource(mod), mod.__name__
