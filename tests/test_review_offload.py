"""Data-safety tests for card → Drive offload. No network: Drive is faked, cards live in tmp_path."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
import time
from pathlib import Path

import pytest
import requests

import camctl.drive as drive_mod
import camctl.offload as off

EMPTY_MD5 = hashlib.md5(b"").hexdigest()


# -- fakes ----------------------------------------------------------------------------------

class FakeDriveStore:
    """Shared state for every FakeDrive instance (offload makes one per worker thread)."""

    def __init__(self) -> None:
        self.folders: dict[str, tuple[str, str]] = {}      # id → (name, parent)
        self.files: dict[str, dict] = {}                   # id → {name, parent, md5Checksum, size}
        self.writes: list[tuple] = []
        self.upload_hook = None                            # (path, meta) → meta
        self.instances = 0
        self.fail_after_instances: int | None = None
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        return f"id{self._n}"

    def add_folder(self, name: str, parent: str = "root") -> str:
        fid = self.new_id()
        self.folders[fid] = (name, parent)
        return fid

    def add_file(self, name: str, parent: str, data: bytes) -> str:
        fid = self.new_id()
        self.files[fid] = {"id": fid, "name": name, "parent": parent, "size": str(len(data)),
                           "md5Checksum": hashlib.md5(data).hexdigest()}
        return fid


def make_fake(store: FakeDriveStore):
    class FakeDrive:
        mode = "fake"

        def __init__(self) -> None:
            store.instances += 1
            if store.fail_after_instances is not None and store.instances > store.fail_after_instances:
                raise drive_mod.DriveError("gcloud token refresh failed")

        def find_folder(self, name, parent="root"):
            for fid, (n, p) in store.folders.items():
                if n == name and p == parent:
                    return fid
            return None

        def ensure_folder(self, name, parent="root"):
            fid = self.find_folder(name, parent)
            if fid:
                return fid
            store.writes.append(("mkdir", name, parent))
            return store.add_folder(name, parent)

        def ensure_path(self, parts, parent="root"):
            for part in parts:
                parent = self.ensure_folder(part, parent)
            return parent

        def walk_md5(self, folder_id):
            out, stack = {}, [folder_id]
            while stack:
                fid = stack.pop()
                stack += [k for k, (_, p) in store.folders.items() if p == fid]
                out.update({f["md5Checksum"]: f for f in store.files.values() if f["parent"] == fid})
            return out

        def children(self, folder_id, folders_only=False, fields=None):
            yield from [f for f in store.files.values() if f["parent"] == folder_id]

        def upload(self, path, folder_id, mime="application/octet-stream", progress=None, name=None):
            data = Path(path).read_bytes()
            store.writes.append(("upload", name or path.name, folder_id))
            meta = dict(store.files[store.add_file(name or path.name, folder_id, data)])
            if store.upload_hook:
                meta = store.upload_hook(path, meta)
            return meta

    return FakeDrive


@pytest.fixture
def store(monkeypatch, tmp_path):
    s = FakeDriveStore()
    monkeypatch.setattr(off, "Drive", make_fake(s))
    monkeypatch.setattr(off, "LOG_DIR", tmp_path / "logs")
    off._MD5_CACHE.clear()
    return s


def make_card(root: Path, files: dict[str, bytes]) -> Path:
    card = root / "Untitled"
    for rel, data in files.items():
        p = card / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return card


def run(card, **kw):
    return off.offload([card], root_name="Root", log=lambda m: None, **kw)


def log_lines(report) -> list[dict]:
    return [json.loads(line) for line in Path(report.log_path).read_text().splitlines()]


# -- safe_to_format_card ---------------------------------------------------------------------

def test_camera_scope_is_not_safe_when_card_has_other_files(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo",
                                "Documents/passport.pdf": b"irreplaceable"})
    r = run(card, scope="camera")
    assert [i.status for i in r.items] == ["uploaded"]
    assert r.safe_to_format is False
    s = r.summary()
    assert s["safe_to_format_card"] is False
    assert any("passport.pdf" in f["file"] for f in s["not_verified_files"])


def test_all_scope_is_safe_once_every_file_is_on_drive(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo",
                                "Documents/passport.pdf": b"irreplaceable",
                                ".DS_Store": b"x", "._DSC00001.JPG": b"x", ".fseventsd/0001": b"x"})
    r = run(card, scope="all")
    assert sorted(i.rel for i in r.items) == ["DCIM/100MSDCF/DSC00001.JPG", "Documents/passport.pdf"]
    assert r.safe_to_format is True and r.summary()["not_verified"] == 0


@pytest.mark.parametrize("extra", [
    {".config.backup": b"hidden file with a 6-char extension"},     # matches the rsync temp pattern
    {".Trashes/501/IMG_0001.HEIC": b"trashed in Finder"},
    {".TemporaryItems/folders.501/draft.txt": b"temp"},
])
def test_files_skipped_by_collect_rules_block_safe(store, tmp_path, extra):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo", **extra})
    r = run(card, scope="all")
    assert not r.failed
    assert r.safe_to_format is False
    assert r.summary()["not_verified"] == 1


def test_symlink_is_not_uploaded_and_blocks_safe(store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not on the card")
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo"})
    (card / "link.txt").symlink_to(outside)
    r = run(card, scope="all")
    assert [i.rel for i in r.items] == ["DCIM/100MSDCF/DSC00001.JPG"]
    assert r.safe_to_format is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_directory_blocks_safe(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo", "locked/secret.txt": b"s"})
    (card / "locked").chmod(0)
    try:
        r = run(card, scope="all")
    finally:
        (card / "locked").chmod(0o755)
    assert r.safe_to_format is False
    assert any("locked" in f["file"] for f in r.summary()["not_verified_files"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_file_is_failed_not_a_crash(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo", "DCIM/100MSDCF/DSC00002.JPG": b"p2"})
    (card / "DCIM/100MSDCF/DSC00002.JPG").chmod(0)
    try:
        r = run(card, scope="camera")
    finally:
        (card / "DCIM/100MSDCF/DSC00002.JPG").chmod(0o644)
    assert {i.rel: i.status for i in r.items} == {"DCIM/100MSDCF/DSC00001.JPG": "uploaded",
                                                 "DCIM/100MSDCF/DSC00002.JPG": "failed"}
    assert r.safe_to_format is False
    assert {(d["rel"], d["status"]) for d in log_lines(r)} >= {("DCIM/100MSDCF/DSC00002.JPG", "failed")}


def test_dcim_folders_past_109_are_camera_media(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/110MSDCF/DSC01000.ARW": b"raw", "DCIM/100MSDCF/DSC00001.ARW": b"r1"})
    assert [p.relative_to(card).as_posix() for p in off.collect(card, "camera")] == [
        "DCIM/100MSDCF/DSC00001.ARW", "DCIM/110MSDCF/DSC01000.ARW"]


def test_dedup_matches_md5_anywhere_in_root_but_never_name_only(store, tmp_path):
    root = store.add_folder("Root")
    elsewhere = store.add_folder("2025-01-01", root)
    store.add_file("renamed.jpg", elsewhere, b"same bytes")               # same md5, other name/folder
    store.add_file("DSC00002.JPG", elsewhere, b"different bytes")         # same name, other md5
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"same bytes",
                                "DCIM/100MSDCF/DSC00002.JPG": b"card bytes"})
    r = run(card, scope="camera")
    assert {i.rel: i.status for i in r.items} == {"DCIM/100MSDCF/DSC00001.JPG": "skipped-existing",
                                                 "DCIM/100MSDCF/DSC00002.JPG": "uploaded"}


def test_zero_byte_file_uploads_and_verifies(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/100MSDCF/EMPTY.JPG": b""})
    r = run(card, scope="camera")
    assert r.items[0].md5 == EMPTY_MD5 and r.items[0].status == "uploaded" and r.safe_to_format


def test_md5_mismatch_after_upload_is_failed(store, tmp_path):
    store.upload_hook = lambda path, meta: {**meta, "md5Checksum": "0" * 32}
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo"})
    r = run(card, scope="camera")
    assert r.items[0].status == "failed" and "md5 mismatch" in r.items[0].error
    assert r.safe_to_format is False and r.summary()["failed"] == 1


def test_file_changed_on_card_after_upload_is_not_verified(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo"})

    def modify(path, meta):
        time.sleep(0.01)
        path.write_bytes(b"photo plus more bytes written later")
        return meta
    store.upload_hook = modify
    r = run(card, scope="camera")
    assert r.items[0].status == "failed"
    assert r.safe_to_format is False


def test_worker_drive_init_failure_is_recorded_not_raised(store, tmp_path):
    store.fail_after_instances = 1                      # main Drive() works, worker Drive() fails
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo"})
    r = run(card, scope="camera")
    assert r.items[0].status == "failed" and "token" in r.items[0].error
    assert log_lines(r)[-1]["status"] == "failed"


def where(store: FakeDriveStore, fid: str) -> str:
    parts = []
    while fid in store.folders:
        parts.append(store.folders[fid][0])
        fid = store.folders[fid][1]
    return "/".join(reversed(parts))


def test_organize_uploads_straight_into_the_year_type_layout(store, tmp_path):
    jpeg = store.add_folder("JPEG", store.add_folder("Camera", store.add_folder("2025", store.add_folder("Root"))))
    store.add_file("DSC00001.JPG", jpeg, b"an older card's DSC00001")          # same name, other content
    card = make_card(tmp_path, {"DCIM/Camera/20190721_183147.jpg": b"phone", "Documents/sub/tax.pdf": b"tax",
                                "DCIM/100MSDCF/DSC00001.JPG": b"new shot", "Music/song.mp3": b"song",
                                "LOST.DIR/1234": b"frag", "PRIVATE/M4ROOT/CLIP/C0001.MP4": b"clip"})
    for p in card.rglob("*"):
        if p.is_file():
            os.utime(p, (1735732800, 1735732800))           # 2025-01-01: the camera wrote the files then
    r = run(card, scope="all", organize=True)
    assert r.safe_to_format and {i.status for i in r.items} == {"uploaded"}
    placed = {f["name"]: where(store, f["parent"]) for f in store.files.values()
              if f["md5Checksum"] in {i.md5 for i in r.items}}
    assert placed == {"20190721_183147.jpg": "Root/2019/Photos", "tax.pdf": "Root/Documents/sub",
                      "DSC00001 (2).JPG": "Root/2025/Camera/JPEG", "song.mp3": "Root/Music",
                      "1234": "Root/Other/LOST.DIR", "C0001.MP4": "Root/2025/Camera/Videos"}
    assert set(log_lines(r)[0]) == {"card", "rel", "size", "md5", "status", "drive_id", "error"}   # log unchanged


# -- dry run ---------------------------------------------------------------------------------

def test_dry_run_makes_zero_drive_writes_when_root_is_missing(store, tmp_path):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo"})
    r = run(card, scope="camera", dry_run=True)
    assert store.writes == []
    assert [i.status for i in r.items] == ["pending"] and r.safe_to_format is False


def test_dry_run_with_existing_root_dedups_without_writes(store, tmp_path):
    store.add_file("x.jpg", store.add_folder("Root"), b"photo")
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"photo"})
    r = run(card, scope="camera", dry_run=True)
    assert store.writes == []
    assert [i.status for i in r.items] == ["skipped-existing"]


# -- JSONL log -------------------------------------------------------------------------------

def test_jsonl_log_final_status_per_file(store, tmp_path):
    store.add_file("x.jpg", store.add_folder("Root"), b"already")
    card = make_card(tmp_path, {"DCIM/100MSDCF/A.JPG": b"already", "DCIM/100MSDCF/B.JPG": b"new",
                                "notes.txt": b"other"})
    r = run(card, scope="camera")
    last = {}
    for d in log_lines(r):
        assert set(d) >= {"card", "rel", "size", "md5", "status", "drive_id", "error"}
        last[d["rel"]] = d["status"]
    assert last == {"DCIM/100MSDCF/A.JPG": "skipped-existing", "DCIM/100MSDCF/B.JPG": "uploaded",
                    "notes.txt": "not-verified"}


# -- follow_upload ---------------------------------------------------------------------------

def test_follow_upload_covers_every_file_not_just_non_camera(monkeypatch, tmp_path):
    calls = []

    class R:
        failed: list = []

        def summary(self):
            return {"files": 0, "uploaded": 0, "already_on_drive": 0, "failed": 0}

    def fake_offload(cards, root_name, scope, jobs, log):
        calls.append(scope)
        return R()
    sentinel = tmp_path / "done"
    sentinel.touch()
    monkeypatch.setattr(off, "offload", fake_offload)
    monkeypatch.setattr(sys, "argv", ["follow_upload.py", str(tmp_path), "Root", str(sentinel)])
    runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "follow_upload.py"), run_name="__main__")
    assert calls == ["all"]


# -- Drive client ----------------------------------------------------------------------------

class Resp:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)
        self.ok = status < 400

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)       # callables (method, url, kw) → Resp, or Resp, or Exception
        self.calls: list[dict] = []

    def _next(self, method, url, kw):
        self.calls.append({"method": method, "url": url, **kw})
        step = self.script.pop(0)
        if callable(step) and not isinstance(step, Resp):
            step = step(method, url, kw)
        if isinstance(step, Exception):
            raise step
        return step

    def request(self, method, url, **kw):
        return self._next(method, url, kw)

    def put(self, url, **kw):
        return self._next("PUT", url, kw)


def bare_drive(script, token_expiry: float | None = None) -> drive_mod.Drive:
    d = drive_mod.Drive.__new__(drive_mod.Drive)
    d.s = FakeSession(script)
    d.mode = "gcloud"
    d._tok = {"access_token": "tok0", "expiry": token_expiry or time.time() + 3600}
    return d


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(drive_mod.time, "sleep", lambda s: None)


def test_chunk_size_is_a_multiple_of_256k():
    assert drive_mod.CHUNK % (256 * 1024) == 0


@pytest.mark.parametrize("name, quoted", [("Sam's photos", r"name = 'Sam\'s photos'"),
                                          ("Family Recipes", "name = 'Family Recipes'"),
                                          ("back\\slash", r"name = 'back\\slash'")])
def test_folder_query_escapes_quotes_and_backslashes(no_sleep, name, quoted):
    d = bare_drive([Resp(200, {"files": [{"id": "F"}]})])
    assert d.ensure_folder(name) == "F"
    assert d.s.calls[0]["params"]["q"].startswith(quoted + " and")


def test_req_retry_keeps_caller_headers(no_sleep):
    d = bare_drive([Resp(503), Resp(200, {}, {"Location": "https://up/session"})])
    d._req("POST", drive_mod.UPLOAD, headers={"X-Upload-Content-Length": "5"})
    assert [c["headers"].get("X-Upload-Content-Length") for c in d.s.calls] == ["5", "5"]


def test_req_retries_network_errors(no_sleep):
    d = bare_drive([requests.ConnectionError("reset"), Resp(200, {"files": []})])
    assert d._req("GET", f"{drive_mod.API}/files").json() == {"files": []}


def test_req_refreshes_token_on_401(no_sleep, monkeypatch):
    monkeypatch.setattr(drive_mod, "_gcloud_token",
                        lambda: {"access_token": "tok1", "expiry": time.time() + 3000})
    d = bare_drive([Resp(401), Resp(200, {"files": []})])
    d._req("GET", f"{drive_mod.API}/files")
    assert [c["headers"]["Authorization"] for c in d.s.calls] == ["Bearer tok0", "Bearer tok1"]


def test_gcloud_token_expiry_comes_from_tokeninfo(monkeypatch):
    class Out:
        returncode, stdout = 0, "cached-token\n"
    monkeypatch.setattr(drive_mod.shutil, "which", lambda n: "/usr/bin/gcloud")
    monkeypatch.setattr(drive_mod.subprocess, "run", lambda *a, **k: Out())
    monkeypatch.setattr(drive_mod.requests, "get",
                        lambda *a, **k: Resp(200, {"scope": drive_mod.SCOPE, "expires_in": "300"}))
    monkeypatch.setenv("CAMCTL_DRIVE_AUTH", "gcloud")
    d = drive_mod.Drive()
    assert d._tok["access_token"] == "cached-token"
    assert d._tok["expiry"] < time.time() + 300          # gcloud hands out cached tokens near expiry


KB256 = 256 * 1024


def upload_start():
    return Resp(200, {}, {"Location": "https://up/session"})


def puts(d):
    return [c for c in d.s.calls if c["method"] == "PUT"]


def test_upload_zero_byte_file(tmp_path):
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    d = bare_drive([upload_start(), Resp(200, {"id": "X", "md5Checksum": EMPTY_MD5})])
    assert d.upload(p, "F")["id"] == "X"
    assert puts(d)[0]["headers"]["Content-Range"] == "bytes */0"


def test_upload_chunks_and_308_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_mod, "CHUNK", KB256)
    p = tmp_path / "f.bin"
    p.write_bytes(os.urandom(KB256 * 2 + 10))
    d = bare_drive([upload_start(), Resp(308, headers={"Range": f"bytes=0-{KB256 - 1}"}),
                    Resp(308, headers={"Range": f"bytes=0-{2 * KB256 - 1}"}), Resp(200, {"id": "X"})])
    d.upload(p, "F")
    ranges = [c["headers"]["Content-Range"] for c in puts(d)]
    assert ranges == [f"bytes 0-{KB256 - 1}/{2 * KB256 + 10}", f"bytes {KB256}-{2 * KB256 - 1}/{2 * KB256 + 10}",
                      f"bytes {2 * KB256}-{2 * KB256 + 9}/{2 * KB256 + 10}"]
    assert [len(c["data"]) for c in puts(d)] == [KB256, KB256, 10]


def test_upload_401_mid_upload_refreshes_token_and_resumes(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(drive_mod, "CHUNK", KB256)
    monkeypatch.setattr(drive_mod, "_gcloud_token",
                        lambda: {"access_token": "tok1", "expiry": time.time() + 3000})
    p = tmp_path / "f.bin"
    p.write_bytes(os.urandom(KB256 + 10))
    d = bare_drive([upload_start(), Resp(308, headers={"Range": f"bytes=0-{KB256 - 1}"}), Resp(401),
                    Resp(308, headers={"Range": f"bytes=0-{KB256 - 1}"}), Resp(200, {"id": "X"})])
    assert d.upload(p, "F")["id"] == "X"
    last = puts(d)[-1]
    assert last["headers"]["Authorization"] == "Bearer tok1"
    assert last["headers"]["Content-Range"] == f"bytes {KB256}-{KB256 + 9}/{KB256 + 10}"


def test_upload_network_error_resumes_from_server_offset_not_zero(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(drive_mod, "CHUNK", KB256)
    p = tmp_path / "f.bin"
    p.write_bytes(os.urandom(KB256 * 2 + 10))
    d = bare_drive([upload_start(), Resp(308, headers={"Range": f"bytes=0-{KB256 - 1}"}),
                    requests.ConnectionError("wifi dropped"),     # 2nd chunk
                    requests.ConnectionError("still down"),       # status query
                    Resp(308, headers={"Range": f"bytes=0-{KB256 - 1}"}),   # status query
                    Resp(308, headers={"Range": f"bytes=0-{2 * KB256 - 1}"}), Resp(200, {"id": "X"})])
    assert d.upload(p, "F")["id"] == "X"
    data_starts = [c["headers"]["Content-Range"].split()[1].split("-")[0] for c in puts(d) if c.get("data")]
    assert "0" not in data_starts[1:]                     # never restarts the file from byte 0


def test_upload_completed_before_network_error_is_not_resent(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(drive_mod, "CHUNK", KB256)
    p = tmp_path / "f.bin"
    p.write_bytes(os.urandom(100))
    d = bare_drive([upload_start(), requests.ReadTimeout("response lost"),
                    Resp(200, {"id": "X", "md5Checksum": "m"})])   # status query: already finalized
    assert d.upload(p, "F")["id"] == "X"
    assert [bool(c.get("data")) for c in puts(d)] == [True, False]


def test_upload_never_sends_bytes_past_declared_size(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(drive_mod, "CHUNK", KB256)
    p = tmp_path / "growing.bin"
    p.write_bytes(os.urandom(KB256 + 100))

    def grow(method, url, kw):
        with p.open("ab") as fh:
            fh.write(os.urandom(KB256))
        return Resp(308, headers={"Range": f"bytes=0-{KB256 - 1}"})
    d = bare_drive([upload_start(), grow, Resp(200, {"id": "X"})])
    d.upload(p, "F")
    last = puts(d)[-1]
    assert last["headers"]["Content-Range"] == f"bytes {KB256}-{KB256 + 99}/{KB256 + 100}"
    assert len(last["data"]) == 100


def test_upload_session_expired_raises(tmp_path, no_sleep):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    d = bare_drive([upload_start(), Resp(410)])
    with pytest.raises(drive_mod.DriveError, match="session"):
        d.upload(p, "F")


# -- persistent md5 cache --------------------------------------------------------------------

def test_second_offload_reads_only_new_files(store, tmp_path, monkeypatch):
    card = make_card(tmp_path, {"DCIM/100MSDCF/DSC00001.JPG": b"one", "DCIM/100MSDCF/DSC00002.JPG": b"two"})
    reads: list[str] = []
    real = off._md5
    monkeypatch.setattr(off, "_md5", lambda p, buf: reads.append(p.name) or real(p, buf))

    first = run(card, scope="all")
    assert first.safe_to_format and sorted(reads) == ["DSC00001.JPG", "DSC00002.JPG"]

    (card / "DCIM/100MSDCF/DSC00003.JPG").write_bytes(b"three")
    off._MD5_CACHE.clear()                       # a new process: only the on-disk cache survives
    reads.clear()
    second = run(card, scope="all")
    assert reads == ["DSC00003.JPG"]             # unchanged files not re-read
    assert second.safe_to_format and len([i for i in second.items if i.status == "uploaded"]) == 1

    reads.clear()
    run(card, scope="all", rehash=True)
    assert len(reads) == 3
