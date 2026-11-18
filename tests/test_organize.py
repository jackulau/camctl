"""`camctl drive organize`: the pure classifier, and organize() against an in-memory Drive. No network."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path

import pytest
import requests

import camctl.drive as drive_mod
import camctl.organize as org
from camctl.organize import classify, free_name, locate, year_source

from .conftest import _HARDWARE_ATTEMPTS
from .test_review_offload import Resp, bare_drive

# A layout like a user's organize.yaml: several kept folders (one multi-word, matched case-insensitively) and a
# music-source folder named differently from the music folder.
TEST_LAYOUT = org.Layout({"documents": "Documents", "projects": "Projects", "family recipes": "Family Recipes"},
                         "Music Library", frozenset({"songs"}))


@pytest.fixture(autouse=True)
def archive_layout(monkeypatch):
    monkeypatch.setattr(org, "_layout_cache", TEST_LAYOUT)

# -- classifier ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("rel, meta, dest", [
    # Sony stills: DCIM/<3 digits><5 chars>/
    ("DCIM/100MSDCF/DSC00001.ARW", {"imageMediaMetadata": {"time": "2026:09:25 10:11:12"}}, ("2026", "Camera", "RAW")),
    ("DCIM/100MSDCF/DSC00001.JPG", {"capture_date": "2025-01-02"}, ("2025", "Camera", "JPEG")),
    ("DCIM/101MSDCF/DSC00002.HIF", {"capture_date": "2025-01-02"}, ("2025", "Camera", "JPEG")),
    ("DCIM/100CANON/IMG_0001.CR3", {"capture_date": "2024-05-06"}, ("2024", "Camera", "RAW")),
    ("DCIM/100_FUJI/DSCF0001.RAF", {"capture_date": "2024-05-06"}, ("2024", "Camera", "RAW")),
    ("DCIM/100MSDCF/DSC00003.ARW", {}, ("Unknown year", "Camera", "RAW")),
    # Sony video + sidecars, proxies
    ("PRIVATE/M4ROOT/CLIP/C0001.MP4", {"sibling_year": 2024}, ("2024", "Camera", "Videos")),
    ("PRIVATE/M4ROOT/CLIP/C0001M01.XML", {"sidecar_time": "2024"}, ("2024", "Camera", "Videos")),
    ("PRIVATE/M4ROOT/SUB/C0001S03.MP4", {"capture_date": "2023-03-03"}, ("2023", "Camera", "Videos")),
    ("PRIVATE/M4ROOT/MEDIAPRO.XML", {}, ("Other", "PRIVATE", "M4ROOT")),
    ("PRIVATE/M4ROOT/THMBNL/C0001T01.JPG", {}, ("Other", "PRIVATE", "M4ROOT", "THMBNL")),
    ("PRIVATE/AVCHD/BDMV/STREAM/00000.MTS", {"capture_date": "2015-01-01"}, ("2015", "Camera", "Videos")),
    # phone media → <year>/Photos|Videos, flat
    ("DCIM/Camera/20190721_183147.jpg", {}, ("2019", "Photos")),
    ("DCIM/Camera/20190721_183147.mp4", {}, ("2019", "Videos")),
    ("DCIM/Camera/IMG_20180102_111111.jpg", {}, ("2018", "Photos")),
    ("Movies/VID_20200101_101010.mp4", {}, ("2020", "Videos")),
    ("Video/clip.3gp", {}, ("Unknown year", "Videos")),
    ("Pictures/Screenshots/Screenshot_20210305-101010.png", {}, ("2021", "Photos")),
    ("Pictures/Screenshot_2022-06-01-10-10-10.png", {}, ("2022", "Photos")),
    ("Download/PXL_20231231_235959123.jpg", {}, ("2023", "Photos")),
    ("Download/IMG-20170808-WA0001.jpeg", {}, ("2017", "Photos")),
    ("Pictures/Instagram/photo.heic", {"imageMediaMetadata": {"time": "2020:02:02 02:02:02"}}, ("2020", "Photos")),
    ("DCIM/100ANDRO/DSC_0001.JPG", {"imageMediaMetadata": {"time": "2014:01:01 00:00:00"}}, ("2014", "Photos")),
    ("Pictures/raw_phone.dng", {"capture_date": "2021-01-01"}, ("2021", "Photos")),
    # user-named folders win, subpaths kept, even for media
    ("Documents/x.pdf", {}, ("Documents",)),
    ("Documents/sub/20190721_183147.jpg", {}, ("Documents", "sub")),
    ("Projects/2019/report.docx", {}, ("Projects", "2019")),
    ("Family Recipes/list.jpg", {}, ("Family Recipes",)),
    ("family recipes/a/b.png", {}, ("Family Recipes", "a")),
    # music
    ("Songs/song.mp3", {}, ("Music Library",)),
    ("Songs/duets/a.m4a", {}, ("Music Library", "duets")),
    ("Songs/lyrics.txt", {}, ("Music Library",)),
    ("Download/track.flac", {}, ("Music Library", "Download")),
    # everything else → Other/<card path>
    ("Download/report.pdf", {}, ("Other", "Download")),
    ("Download/app.apk", {}, ("Other", "Download")),
    ("Movies/subs.srt", {}, ("Other", "Movies")),
    ("LOST.DIR/12345", {}, ("Other", "LOST.DIR")),
    ("LOST.DIR/1234.jpg", {}, ("Other", "LOST.DIR")),
    ("Android/data/com.x/cache/a.jpg", {}, ("Other", "Android", "data", "com.x", "cache")),
    ("Android/media/com.x/notif.mp3", {}, ("Other", "Android", "media", "com.x")),
    ("AVF_INFO/AVIN0001.BNP", {}, ("Other", "AVF_INFO")),
    ("RW_LIB/lib.dat", {}, ("Other", "RW_LIB")),
    ("DCIM/.thumbnails/1.jpg", {}, ("Other", "DCIM", ".thumbnails")),
    ("DCIM/100MSDCF/DSC00001.THM", {}, ("Other", "DCIM", "100MSDCF")),
    ("notes.txt", {}, ("Other",)),
])
def test_classify(rel, meta, dest):
    assert classify(rel, meta=meta) == dest


@pytest.mark.parametrize("name, meta, expected", [
    ("x.jpg", {"imageMediaMetadata": {"time": "2011:01:01 00:00:00"}}, (2011, "exif")),
    ("20190721_183147.jpg", {"imageMediaMetadata": {"time": "2011:01:01 00:00:00"}}, (2011, "exif")),   # EXIF first
    ("20190721_183147.jpg", {"imageMediaMetadata": {"time": "0000:00:00 00:00:00"}}, (2019, "filename")),
    ("19800101_000000.jpg", {}, (None, "unknown")),                        # before 1995: unset camera clock
    ("20991231_000000.jpg", {}, (None, "unknown")),                        # future
    ("x.jpg", {"imageMediaMetadata": {"time": "1980:01:01 00:00:00"}, "capture_date": "2016-01-01"},
     (2016, "capture-date")),
    ("DSC00001.JPG", {"sidecar_time": "2013", "capture_date": "2016-01-01"}, (2013, "sidecar")),
    ("x.jpg", {"mtime": 1500000000}, (2017, "mtime")),
    # Drive modifiedTime only counts when clearly older than createdTime (the upload): preserved from the file
    ("x.jpg", {"modifiedTime": "2012-05-05T00:00:00Z", "createdTime": "2026-09-25T00:00:00Z"}, (2012, "modified-time")),
    ("x.jpg", {"modifiedTime": "2026-09-25T00:00:05Z", "createdTime": "2026-09-25T00:00:00Z"}, (None, "unknown")),
    ("x.jpg", {"sibling_year": 2010}, (2010, "sibling")),
    ("Untitled-2026-09-25.jpg", {}, (2026, "filename")),
])
def test_year_source(name, meta, expected):
    assert year_source(name, meta) == expected


@pytest.mark.parametrize("parts, name, expected", [
    (("Untitled-2026-09-25", "DCIM", "Camera"), "a.jpg", ("card-copy", "DCIM/Camera/a.jpg", None)),
    (("Untitled-2026-09-26",), "notes.txt", ("card-copy", "notes.txt", None)),
    (("2026-09-25", "Untitled", "DCIM", "100MSDCF"), "D.ARW", ("dated", "DCIM/100MSDCF/D.ARW", "2026-09-25")),
    (("2019", "Photos"), "a.jpg", ("organized", "", None)),
    (("Unknown year", "Camera", "RAW"), "a.ARW", ("organized", "", None)),
    (("Documents", "sub"), "x.pdf", ("organized", "", None)),
    (("Other", "Android"), "x", ("organized", "", None)),
    (("2019", "Photos", "deeper"), "a.jpg", ("unrecognized", "2019/Photos/deeper/a.jpg", None)),
    ((), "loose.jpg", ("unrecognized", "loose.jpg", None)),
])
def test_locate(parts, name, expected):
    assert locate(parts, name) == expected


def test_free_name():
    assert free_name("a.jpg", {}) == "a.jpg"
    assert free_name("a.jpg", {"a.jpg": "m1"}) == "a (2).jpg"
    assert free_name("A.JPG", {"a.jpg": "m1", "a (2).jpg": "m2"}) == "A (3).JPG"     # case-insensitive
    assert free_name("C0001M01.XML", {"c0001m01.xml": "m"}, prefer=4) == "C0001M01 (4).XML"
    assert free_name(".nomedia", {".nomedia": "m"}) == ".nomedia (2)"


# -- fake Drive -------------------------------------------------------------------------------------

def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class Store:
    """Drive state shared by every FakeDrive instance (organize makes one per worker thread)."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.content: dict[str, bytes] = {}
        self.fail_update: set[str] = set()          # file ids whose update raises
        self.ignore_move: set[str] = set()          # file ids whose update "succeeds" but changes nothing
        self.lock = threading.Lock()
        self._n = 0
        self._t = 0

    def _id(self) -> str:
        self._n += 1
        return f"id{self._n}"

    def folder(self, name: str, parent: str = "root") -> str:
        fid = self._id()
        self.items[fid] = {"id": fid, "name": name, "parent": parent, "folder": True, "trashed": False}
        return fid

    def path(self, parts: list[str], parent: str = "root") -> str:
        for p in parts:
            found = [i for i, f in self.items.items() if f["folder"] and f["name"] == p and f["parent"] == parent
                     and not f["trashed"]]
            parent = found[0] if found else self.folder(p, parent)
        return parent

    def file(self, where: str, data: bytes, created: str | None = None, **meta) -> str:
        *dirs, name = where.split("/")
        parent = self.path(dirs)
        fid = self._id()
        self._t += 1
        self.items[fid] = {"id": fid, "name": name, "parent": parent, "folder": False, "trashed": False,
                           "md5Checksum": meta.pop("md5", md5(data)), "size": str(len(data)),
                           "createdTime": created or f"2026-09-25T00:00:{self._t:02d}Z",
                           "modifiedTime": created or f"2026-09-25T00:00:{self._t:02d}Z", **meta}
        self.content[fid] = data
        return fid

    def alive(self, fid: str) -> bool:
        while fid in self.items:
            if self.items[fid]["trashed"]:
                return False
            fid = self.items[fid]["parent"]
        return True

    def where(self, fid: str) -> str:
        parts = []
        while fid in self.items:
            parts.append(self.items[fid]["name"])
            fid = self.items[fid]["parent"]
        return "/".join(reversed(parts))

    def live_files(self) -> dict[str, str]:
        """path → md5 of every non-trashed file."""
        return {self.where(i): f["md5Checksum"] for i, f in self.items.items()
                if not f["folder"] and self.alive(i)}


def make_fake(store: Store):
    class FakeDrive:
        mode = "fake"

        def find_folder(self, name, parent="root"):
            for i, f in store.items.items():
                if f["folder"] and f["name"] == name and f["parent"] == parent and not f["trashed"]:
                    return i
            return None

        def ensure_folder(self, name, parent="root"):
            with store.lock:
                fid = self.find_folder(name, parent)
                if not fid:
                    store.calls.append(("mkdir", name, parent))
                    fid = store.folder(name, parent)
                return fid

        def children(self, folder_id, folders_only=False, fields=None):
            for i, f in list(store.items.items()):
                if f["parent"] == folder_id and not f["trashed"] and (f["folder"] or not folders_only):
                    out = {k: v for k, v in f.items() if k not in ("parent", "folder", "trashed")}
                    out["mimeType"] = drive_mod.FOLDER_MIME if f["folder"] else "image/jpeg"
                    out["parents"] = [folder_id]
                    yield out

        walk_tree = drive_mod.Drive.walk_tree         # the real traversal over the fake children()

        def update(self, file_id, *, add_parent=None, remove_parent=None, name=None, trashed=None):
            with store.lock:
                store.calls.append(("update", file_id, add_parent, remove_parent, name, trashed))
                if file_id in store.fail_update:
                    raise drive_mod.DriveError("PATCH → 500 backendError (kept failing)")
                if file_id in store.ignore_move:
                    return {}
                f = store.items[file_id]
                if remove_parent:
                    assert f["parent"] == remove_parent
                if add_parent:
                    f["parent"] = add_parent
                if name is not None:
                    f["name"] = name
                if trashed is not None:
                    f["trashed"] = trashed
                return {"id": file_id}

        def trash(self, file_id):
            return self.update(file_id, trashed=True)

        def read_head(self, file_id, nbytes=65536):
            store.calls.append(("read", file_id))
            return store.content[file_id][:nbytes]

    return FakeDrive


@pytest.fixture
def store(monkeypatch):
    s = Store()
    monkeypatch.setattr(drive_mod, "Drive", make_fake(s))
    monkeypatch.setattr(org, "offload_running", lambda: [])
    return s


def run(store, apply=True, merge=("A7V Offload",), **kw):
    return org.organize("Card Archive", merge=merge, dry_run=not apply, log=lambda m: None, **kw)


XML = b'<?xml version="1.0"?><NonRealTimeMeta><CreationDate value="2024-07-01T10:00:00+02:00"/></NonRealTimeMeta>'


def card_archive(store: Store) -> None:
    """Card Archive as scripts/follow_upload.py and a running scope=all offload leave it, plus A7V Offload."""
    store.folder("Card Archive")
    store.folder("A7V Offload")
    cp = "Card Archive/Untitled-2026-09-25"
    store.file(f"{cp}/DCIM/Camera/20190721_183147.jpg", b"phone photo")
    store.file(f"{cp}/DCIM/Camera/20190721_183147.mp4", b"phone video")
    store.file(f"{cp}/DCIM/100MSDCF/DSC00001.ARW", b"raw1", imageMediaMetadata={"time": "2025:03:04 05:06:07"})
    store.file(f"{cp}/DCIM/100MSDCF/DSC00001.JPG", b"jpg1")                       # year from its ARW sibling
    store.file(f"{cp}/PRIVATE/M4ROOT/CLIP/C0001.MP4", b"clip1")                   # year from the XML sidecar
    store.file(f"{cp}/PRIVATE/M4ROOT/CLIP/C0001M01.XML", XML)
    store.file(f"{cp}/Documents/sub/tax.pdf", b"tax")
    store.file(f"{cp}/Family Recipes/list.jpg", b"recipe")
    store.file(f"{cp}/Songs/song.mp3", b"song")
    store.file(f"{cp}/Android/data/x.bin", b"app data")
    store.file(f"{cp}/LOST.DIR/1234", b"fragment")
    store.file("Card Archive/Untitled-2026-09-26/Download/IMG_20180102_111111.jpg", b"download photo")
    store.file("A7V Offload/2026-09-24/Untitled/DCIM/100MSDCF/DSC09999.JPG", b"a7v jpg")


# -- organize: plan, apply, verification ------------------------------------------------------------------

def test_dry_run_plans_but_writes_nothing(store):
    card_archive(store)
    before = copy.deepcopy(store.items)
    r = run(store, apply=False)
    s = r.summary()
    assert [c for c in store.calls if c[0] != "read"] == [] and store.items == before
    assert s["dry_run"] is True and s["ok"] is True and s["files"] == 13 and s["moves"] == 13
    assert s["destinations"]["2019/Photos"] == 1 and s["destinations"]["2026/Camera/JPEG"] == 1
    assert s["destinations"]["Documents"] == 1 and s["destinations"]["Other"] == 2
    assert s["layouts"] == {"card-copy": 12, "dated": 1}
    assert s["folders_emptied_to_trash"] == 3          # Untitled-2026-09-25, Untitled-2026-09-26, A7V Offload
    assert {"from": "Card Archive/Untitled-2026-09-25/DCIM/Camera/20190721_183147.jpg",
            "to": "Card Archive/2019/Photos/20190721_183147.jpg"} in s["sample_moves"]
    json.dumps(s)


def test_apply_moves_everything_into_year_type_layout(store):
    card_archive(store)
    ids_before = {i: f["md5Checksum"] for i, f in store.items.items() if not f["folder"]}
    r = run(store)
    assert r.ok and r.verification["ok"], r.summary()
    assert store.live_files() == {
        "Card Archive/2019/Photos/20190721_183147.jpg": md5(b"phone photo"),
        "Card Archive/2019/Videos/20190721_183147.mp4": md5(b"phone video"),
        "Card Archive/2025/Camera/RAW/DSC00001.ARW": md5(b"raw1"),
        "Card Archive/2025/Camera/JPEG/DSC00001.JPG": md5(b"jpg1"),
        "Card Archive/2024/Camera/Videos/C0001.MP4": md5(b"clip1"),
        "Card Archive/2024/Camera/Videos/C0001M01.XML": md5(XML),
        "Card Archive/Documents/sub/tax.pdf": md5(b"tax"),
        "Card Archive/Family Recipes/list.jpg": md5(b"recipe"),
        "Card Archive/Music Library/song.mp3": md5(b"song"),
        "Card Archive/Other/Android/data/x.bin": md5(b"app data"),
        "Card Archive/Other/LOST.DIR/1234": md5(b"fragment"),
        "Card Archive/2018/Photos/IMG_20180102_111111.jpg": md5(b"download photo"),
        "Card Archive/2026/Camera/JPEG/DSC09999.JPG": md5(b"a7v jpg"),        # merged from A7V Offload
    }
    # moved, not re-uploaded: the same file ids, same md5s, nothing new
    assert {i: f["md5Checksum"] for i, f in store.items.items() if not f["folder"]} == ids_before
    # emptied folders are trashed (recoverable), never removed
    for name in ("Untitled-2026-09-25", "Untitled-2026-09-26", "A7V Offload"):
        assert [f["trashed"] for f in store.items.values() if f["folder"] and f["name"] == name] == [True]
    assert not store.items[store.path(["Card Archive"])]["trashed"]
    ops = [json.loads(line)["op"] for line in Path(r.log_path).read_text().splitlines()]
    assert ops.count("move") == 13 and ops[0] == "plan" and "verify" in ops and "trash-folder" in ops
    assert Path(r.log_path).name.startswith("organize-")


def test_second_run_is_a_no_op(store):
    card_archive(store)
    assert run(store).ok
    n = len(store.calls)
    r = run(store)
    s = r.summary()
    assert s["moves"] == 0 and s["duplicates_to_trash"] == 0 and s["already_in_place"] == 13
    assert s["folders_emptied_to_trash"] == 0 and r.ok
    assert [c for c in store.calls[n:] if c[0] in ("update", "mkdir")] == []


def test_duplicates_keep_one_and_trash_the_rest(store):
    store.folder("Card Archive")
    store.folder("A7V Offload")
    old = store.file("A7V Offload/2026-09-23/Untitled/DCIM/100MSDCF/DSC00005.JPG", b"same", "2026-09-23T00:00:00Z")
    new = store.file("Card Archive/Untitled-2026-09-25/DCIM/100MSDCF/DSC00005.JPG", b"same", "2026-09-25T00:00:00Z")
    r = run(store)
    assert r.ok and r.trashed_duplicates == 1
    assert store.items[new]["trashed"] is True and new in store.items          # trashed, not deleted
    assert store.alive(old) and store.where(old) == "Card Archive/2026/Camera/JPEG/DSC00005.JPG"


def test_duplicate_already_in_place_wins_over_older_copy(store):
    store.folder("Card Archive")
    placed = store.file("Card Archive/2019/Photos/20190721_183147.jpg", b"p", "2026-09-26T00:00:00Z")
    older = store.file("Card Archive/Untitled-2026-09-25/DCIM/Camera/20190721_183147.jpg", b"p",
                       "2026-09-25T00:00:00Z")
    r = run(store, merge=())
    assert r.ok and store.alive(placed) and not store.alive(older)
    assert store.where(placed) == "Card Archive/2019/Photos/20190721_183147.jpg"
    assert r.plan["moves"] == 0


def test_duplicate_in_user_named_folder_wins(store):
    store.folder("Card Archive")
    store.file("Card Archive/Untitled-2026-09-25/DCIM/Camera/20190721_183147.jpg", b"p", "2026-09-01T00:00:00Z")
    store.file("Card Archive/Untitled-2026-09-25/Family Recipes/pie.jpg", b"p", "2026-09-02T00:00:00Z")
    assert run(store, merge=()).ok
    assert store.live_files() == {"Card Archive/Family Recipes/pie.jpg": md5(b"p")}


def test_zero_byte_files_are_not_deduplicated_across_paths(store):
    store.folder("Card Archive")
    store.file("Card Archive/Untitled-2026-09-25/Android/a/.nomedia", b"")
    store.file("Card Archive/Untitled-2026-09-25/Android/b/.nomedia", b"")
    store.file("Card Archive/Untitled-2026-09-26/Android/b/.nomedia", b"")       # same path + content: a copy
    r = run(store, merge=())
    assert r.ok and r.trashed_duplicates == 1
    assert sorted(store.live_files()) == ["Card Archive/Other/Android/a/.nomedia",
                                          "Card Archive/Other/Android/b/.nomedia"]


def test_name_clash_gets_numbered_and_sidecar_follows_its_clip(store):
    store.folder("Card Archive")
    store.file("Card Archive/2024/Camera/Videos/C0001.MP4", b"old clip")                       # already placed
    store.file("Card Archive/2024/Camera/Videos/C0001M01.XML", b'<CreationDate value="2024-01-01"/>')
    store.file("Card Archive/Untitled-2026-09-25/PRIVATE/M4ROOT/CLIP/C0001.MP4", b"new clip")
    store.file("Card Archive/Untitled-2026-09-25/PRIVATE/M4ROOT/CLIP/C0001M01.XML", XML)
    r = run(store, merge=())
    assert r.ok
    assert sorted(store.live_files()) == ["Card Archive/2024/Camera/Videos/C0001 (2).MP4",
                                          "Card Archive/2024/Camera/Videos/C0001.MP4",
                                          "Card Archive/2024/Camera/Videos/C0001M01 (2).XML",
                                          "Card Archive/2024/Camera/Videos/C0001M01.XML"]
    assert r.plan["renamed_for_name_clash"] == 2


def test_folder_holding_a_file_without_md5_is_never_trashed(store):
    store.folder("Card Archive")
    store.folder("A7V Offload")
    store.file("A7V Offload/2026-09-24/Untitled/DCIM/100MSDCF/DSC00001.JPG", b"j")
    doc = store.file("A7V Offload/notes/My Google Doc", b"", md5=None)
    store.file("Card Archive/Untitled-2026-09-25/keep/Google Sheet", b"", md5=None)
    r = run(store)
    assert r.ok and r.plan["not_touched_no_md5"] == 2
    assert store.alive(doc) and store.where(doc) == "A7V Offload/notes/My Google Doc"
    live = store.live_files()
    assert "Card Archive/Untitled-2026-09-25/keep/Google Sheet" in live
    trashed = {f["name"] for f in store.items.values() if f["folder"] and f["trashed"]}
    assert trashed == {"2026-09-24"}                        # A7V Offload and notes/ keep the Google Doc


def test_organized_folders_are_never_trashed_even_if_empty(store):
    card_archive(store)
    store.path(["Card Archive", "2011", "Photos"])                    # empty, already in the organized layout
    store.path(["Card Archive", "Other", "empty"])
    s = run(store, apply=False).summary()
    assert s["folders_emptied_to_trash"] == 3
    assert run(store).ok
    assert {f["name"] for f in store.items.values() if f["folder"] and f["trashed"]} == {
        "Untitled-2026-09-25", "Untitled-2026-09-26", "A7V Offload"}


def test_failed_move_is_reported_and_rerun_finishes(store, capsys):
    card_archive(store)
    stuck = next(i for i, f in store.items.items() if f["name"] == "DSC09999.JPG")
    store.fail_update.add(stuck)
    r = run(store)
    assert not r.ok and r.summary()["failed"] == 1
    assert r.verification["still_in_merge_folder_count"] == 1 and r.verification["missing_count"] == 0
    assert r.trashed_folders == 0                            # no folder cleanup after a failed run
    store.fail_update.clear()
    r2 = run(store)
    assert r2.ok and r2.moved == 1 and store.where(stuck) == "Card Archive/2026/Camera/JPEG/DSC09999.JPG"


def test_verification_catches_a_move_that_silently_did_nothing(store):
    card_archive(store)
    store.ignore_move.add(next(i for i, f in store.items.items() if f["name"] == "DSC09999.JPG"))
    r = run(store)
    assert not r.ok and r.verification["still_in_merge_folder_count"] == 1


def test_verification_reports_missing_files(store, monkeypatch):
    card_archive(store)
    victim = next(i for i, f in store.items.items() if f["name"] == "tax.pdf")
    real = org._walk

    def walk_after_file_vanished(drive, areas):
        if store.calls:                                       # the verification walk, after the moves
            store.items[victim]["trashed"] = True
        return real(drive, areas)
    monkeypatch.setattr(org, "_walk", walk_after_file_vanished)
    r = run(store)
    assert not r.ok and r.verification["missing"] == [md5(b"tax")]


def test_apply_refuses_while_an_offload_runs(store, monkeypatch):
    card_archive(store)
    monkeypatch.setattr(org, "offload_running", lambda: ["123 camctl --json offload --scope all"])
    with pytest.raises(org.OrganizeError, match="offload is still running"):
        run(store)
    assert [c for c in store.calls if c[0] in ("update", "mkdir")] == []
    assert run(store, apply=False).plan["moves"] == 13        # a dry run is fine meanwhile
    assert run(store, allow_running_offload=True).ok


def test_offload_running_detection(monkeypatch):
    class Out:
        stdout = ("  101 /Users/j/camctl/.venv/bin/python3 /Users/j/camctl/.venv/bin/camctl --json offload --src x\n"
                  "  102 tail -F /Users/j/.config/camctl/offload-logs/run.log\n"
                  "  103 camctl-mcp\n  104 python scripts/follow_upload.py /tmp/x Root /tmp/done\n"
                  "  105 camctl drive organize --apply\n")
    monkeypatch.setattr(org.subprocess, "run", lambda *a, **k: Out())
    assert [line.split()[0] for line in org.offload_running()] == ["101", "104"]


def test_missing_root_is_an_error(store):
    with pytest.raises(org.OrganizeError, match="Card Archive"):
        run(store, apply=False)


def test_merge_folder_not_found_is_reported(store):
    store.folder("Card Archive")
    s = run(store, apply=False, merge=("Nope",)).summary()
    assert s["merge_not_found"] == ["Nope"] and s["files"] == 0


# -- CLI / MCP ----------------------------------------------------------------------------------------------

def cli(argv, capsys):
    from camctl import cli as cli_mod
    try:
        cli_mod.main(argv)
        code = 0
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_dry_run_then_apply(store, capsys):
    card_archive(store)
    code, out, _ = cli(["drive", "organize"], capsys)
    assert code == 0 and "Dry run: nothing changed" in out and "2019/Photos" in out
    assert [c for c in store.calls if c[0] in ("update", "mkdir")] == []
    code, out, _ = cli(["--json", "drive", "organize", "--apply"], capsys)
    data = json.loads(out)
    assert code == 0 and data["ok"] and data["moved"] == 13 and data["verification"]["ok"]


def test_cli_exit_1_when_verification_fails(store, capsys):
    card_archive(store)
    store.fail_update.add(next(i for i, f in store.items.items() if f["name"] == "song.mp3"))
    code, out, _ = cli(["drive", "organize", "--apply", "--no-merge"], capsys)
    assert code == 1 and "FAILED" in out and "Re-run" in out


def test_cli_merge_flags(store, capsys, monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        raise SystemExit(0)
    monkeypatch.setattr(org, "organize", fake)
    cli(["drive", "organize", "--merge", "Old A", "--merge", "Old B"], capsys)
    assert seen["merge"] == ["Old A", "Old B"] and seen["dry_run"] is True and seen["root_name"] == "Card Archive"
    cli(["drive", "organize", "--no-merge", "--root", "X"], capsys)
    assert seen["merge"] == [] and seen["root_name"] == "X"
    cli(["drive", "organize"], capsys)
    assert seen["merge"] == ["A7V Offload"]


def test_mcp_drive_organize_defaults_to_dry_run(store, capsys):
    from camctl import mcp_server as srv
    card_archive(store)
    r = srv.drive_organize()
    assert r["dry_run"] is True and r["moves"] == 13 and r["merge"] == ["A7V Offload"]
    assert [c for c in store.calls if c[0] in ("update", "mkdir")] == []
    assert capsys.readouterr().out == ""                    # stdout is the MCP channel
    assert srv.drive_organize(apply=True)["ok"] is True


# -- Drive client additions --------------------------------------------------------------------------------

def test_update_moves_by_parents_and_trash_sets_trashed(monkeypatch):
    d = bare_drive([Resp(200, {"id": "F"}), Resp(200, {"id": "F"})])
    d.update("F", add_parent="NEW", remove_parent="OLD", name="a (2).jpg")
    d.trash("F")
    move, trash = d.s.calls
    assert move["method"] == "PATCH" and move["url"].endswith("/files/F")
    assert move["params"]["addParents"] == "NEW" and move["params"]["removeParents"] == "OLD"
    assert move["json"] == {"name": "a (2).jpg"}
    assert trash["json"] == {"trashed": True} and "addParents" not in trash["params"]


def test_req_retries_403_rate_limit_but_not_other_403(monkeypatch):
    monkeypatch.setattr(drive_mod.time, "sleep", lambda s: None)
    d = bare_drive([Resp(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}), Resp(200, {"id": "F"})])
    assert d.update("F", trashed=True) == {"id": "F"}
    d = bare_drive([Resp(403, {"error": {"errors": [{"reason": "insufficientFilePermissions"}]}})])
    with pytest.raises(drive_mod.DriveError, match="403"):
        d.update("F", trashed=True)


def test_walk_tree_paths_and_fields():
    folder = drive_mod.FOLDER_MIME
    d = bare_drive([
        Resp(200, {"files": [{"id": "D", "name": "DCIM", "mimeType": folder},
                             {"id": "f1", "name": "top.txt", "mimeType": "text/plain", "md5Checksum": "a"}]}),
        Resp(200, {"files": [{"id": "f2", "name": "x.jpg", "mimeType": "image/jpeg", "md5Checksum": "b"}]}),
    ])
    files, folders = d.walk_tree("ROOT")
    assert {f["id"]: (f["_path"], f["_parent"]) for f in files} == {"f1": ((), "ROOT"), "f2": (("DCIM",), "D")}
    assert folders == {"D": {"id": "D", "name": "DCIM", "parent": "ROOT", "path": ("DCIM",)}}
    assert "imageMediaMetadata(time)" in d.s.calls[0]["params"]["fields"]


def test_upload_sends_mtime_and_name_override(tmp_path):
    p = tmp_path / "f.jpg"
    p.write_bytes(b"abc")
    import os
    os.utime(p, (1563734567, 1563734567))
    d = bare_drive([Resp(200, {}, {"Location": "https://up/s"}), Resp(200, {"id": "X"})])
    d.upload(p, "F", name="f (2).jpg")
    body = d.s.calls[0]["json"]
    assert body["name"] == "f (2).jpg" and body["modifiedTime"].startswith("2019-07-21T")


def test_tests_cannot_reach_google():
    with pytest.raises(AssertionError, match="network"):
        requests.get("https://www.googleapis.com/drive/v3/about")
    assert _HARDWARE_ATTEMPTS
    _HARDWARE_ATTEMPTS.clear()
    with pytest.raises(drive_mod.DriveError):                  # and no credentials are picked up either
        drive_mod.Drive()


# -- layout config (organize.yaml) ------------------------------------------------------------------

def test_layout_defaults_without_a_config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(org, "_layout_cache", None)            # conftest points the config at a missing file
    assert org.layout() == org.DEFAULT_LAYOUT
    assert classify("Music/a.mp3") == ("Music",) and classify("Music/notes.txt") == ("Music",)
    assert classify("Download/a.mp3") == ("Music", "Download")
    assert classify("documents/x/y.pdf") == ("Documents", "x")
    assert classify("Projects/x.pdf") == ("Other", "Projects")    # not kept unless configured


def test_layout_loads_organize_yaml(tmp_path):
    cfg = tmp_path / "organize.yaml"
    cfg.write_text("keep_folders: [Documents, Family Recipes]\nmusic_folder: Music Library\nmusic_sources: [Songs]\n")
    lay = org.load_layout(cfg)
    assert lay.keep == {"documents": "Documents", "family recipes": "Family Recipes"}
    assert lay.music == "Music Library" and lay.music_sources == {"songs"}
    assert org.load_layout(tmp_path / "missing.yaml") == org.DEFAULT_LAYOUT


def test_layout_env_var_overrides_path(monkeypatch, tmp_path):
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("music_folder: Audio\n")
    monkeypatch.setenv("CAMCTL_ORGANIZE_CONFIG", str(cfg))
    monkeypatch.setattr(org, "_layout_cache", None)
    assert org.layout().music == "Audio" and org.layout().music_sources == {"audio"}


@pytest.mark.parametrize("text", [
    "keep_folder: [Documents]\n",                 # typo'd key: refuse rather than silently re-layout an archive
    "keep_folders: Documents\n",                  # not a list
    "keep_folders: [a/b]\n",                      # nested path
    "music_folder: ''\n",
    "- just a list\n",
    "keep_folders: [unclosed\n",                  # YAML syntax error
])
def test_malformed_layout_raises_instead_of_falling_back(tmp_path, text):
    cfg = tmp_path / "organize.yaml"
    cfg.write_text(text)
    with pytest.raises(org.OrganizeError, match="organize.yaml"):
        org.load_layout(cfg)
