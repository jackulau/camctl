"""Offload a Sony memory card to Google Drive with md5 verification.

Card layout (Sony α):
  DCIM/1xxMSDCF/*.ARW|JPG|HIF      stills
  PRIVATE/M4ROOT/CLIP/*.MP4|XML    XAVC clips + metadata sidecars
  PRIVATE/M4ROOT/SUB/*.MP4         proxy clips (if enabled)

Drive layout:  organize=True (the CLI/MCP default): the year/type layout of `camctl drive organize`
                 (<root>/<YYYY>/Photos|Videos|Camera/…, Documents/…, Other/<relative path>, …)
               organize=False ("dated"): scope camera → <root>/<capture date>/<card label>/<relative path>,
                 scope other|all → <root>/<card label>-<today>/<relative path>
Dedup:         any file under <root> with the same md5 is skipped, so re-running
               after shooting more only uploads the new files.

Nothing is ever deleted from the card. When every regular file on the card (any scope)
is md5-verified on Drive the report says so; clear the card by formatting it IN CAMERA (keeps Sony's clip
database consistent, unlike deleting files over USB).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import plistlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .paths import CONFIG_DIR
from .drive import Drive
from .organize import classify, free_name

MEDIA_DIRS = (("PRIVATE", "M4ROOT", "CLIP"), ("PRIVATE", "M4ROOT", "SUB"))   # plus every DCIM/<folder>/
SKIP_NAMES = {".DS_Store"}                        # macOS metadata: ignored, never blocks safe_to_format
SKIP_DIRS = {".fseventsd", ".Spotlight-V100"}     # (so are ._* AppleDouble files)
LOG_DIR = CONFIG_DIR / "offload-logs"


def find_cards(volumes: Path = Path("/Volumes")) -> list[Path]:
    """Mounted volumes that look like a Sony camera card."""
    cards = []
    for v in sorted(volumes.iterdir()) if volumes.exists() else []:
        if (v / "DCIM").is_dir() or (v / "PRIVATE" / "M4ROOT").is_dir():
            cards.append(v)
    return cards


def _is_media(rel: Path) -> bool:
    """DCIM/<any folder>/… (100MSDCF-999MSDCF or date-named) and the XAVC clip folders."""
    parts = rel.parts
    return (len(parts) > 2 and parts[0] == "DCIM") or (len(parts) > 3 and parts[:3] in MEDIA_DIRS)


def _skip_reason(p: Path, rel: Path, scope: str) -> str | None:
    """None → offload it; "" → macOS metadata, ignore; otherwise why this file is not verified by this run."""
    if p.name in SKIP_NAMES or p.name.startswith("._"):
        return ""
    if p.is_symlink():
        return "symlink: not uploaded"
    if not p.is_file():
        return "not a regular file"
    if rel.parts[0] in (".Trashes", ".TemporaryItems"):
        return f"in {rel.parts[0]}: not uploaded"
    if RSYNC_TEMP.match(p.name):
        return "looks like an in-flight rsync temp file: not uploaded"
    if scope != "all" and (scope == "camera") != _is_media(rel):
        return f"outside scope={scope}: not checked (use scope=all)"
    return None


def scan(card: Path, scope: str = "camera") -> tuple[list[Path], list[tuple[str, str]]]:
    """(files to offload, [(rel, reason)] for every other file on the card that this run cannot verify)."""
    if scope not in ("camera", "other", "all"):
        raise ValueError(f"scope must be camera | other | all, not {scope!r}")
    files: list[Path] = []
    unverified: list[tuple[str, str]] = []

    def unreadable(err: OSError) -> None:
        unverified.append((os.path.relpath(err.filename, card), f"unreadable directory: {err.strerror}"))

    for dirpath, dirnames, filenames in os.walk(card, onerror=unreadable):
        here = Path(dirpath)
        if here == card:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in [d for d in dirnames if (here / d).is_symlink()] + filenames:
            p = here / name
            rel = p.relative_to(card)
            reason = _skip_reason(p, rel, scope)
            if reason is None:
                files.append(p)
            elif reason:
                unverified.append((str(rel), reason))
    return sorted(files), sorted(unverified)


def collect(card: Path, scope: str = "camera") -> list[Path]:
    """scope='camera': Sony-shot media only. scope='other': everything on the card EXCEPT camera media
    (e.g. leftovers from a phone that used the card before). scope='all': both."""
    return scan(card, scope)[0]


RSYNC_TEMP = re.compile(r"^\..+\.[A-Za-z0-9]{6}$")   # in-flight rsync file: .name.XXXXXX
_MD5_CACHE: dict[tuple[str, int, int], str] = {}


def md5sum(path: Path, buf: int = 8 * 1024 * 1024) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key in _MD5_CACHE:
        return _MD5_CACHE[key]
    _MD5_CACHE[key] = _md5(path, buf)
    return _MD5_CACHE[key]


HASH_CACHE = CONFIG_DIR / "md5-cache.json"
HASH_CACHE_DAYS = 180                             # forget a card not seen for this long (e.g. reformatted)


def volume_id(card: Path) -> str:
    """Stable identity of the card's filesystem (exFAT/FAT volume serial, changes when reformatted).

    Keeps two cards that are both labelled "Untitled" from sharing cached hashes."""
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(card)], capture_output=True, timeout=10, check=True)
        info = plistlib.loads(out.stdout)
        uid = info.get("VolumeUUID") or info.get("DiskUUID")
        if uid:
            return f"uuid:{uid}"
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        pass
    return f"name:{card.name}"


class HashCache:
    """Persistent md5 per (card volume, card-relative path, size, mtime_ns).

    A card file whose size and modification time are unchanged since it was last hashed is not read again,
    so re-offloading a card only reads the new files. `rehash=True` ignores (but still refreshes) the cache."""

    def __init__(self, path: Path | None = None, rehash: bool = False) -> None:
        self.path, self.rehash, self.hits = path or HASH_CACHE, rehash, 0
        self._lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data.get("volumes"), dict):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            self.data = {"version": 1, "volumes": {}}

    def _files(self, vol: str) -> dict:
        v = self.data["volumes"].setdefault(vol, {"files": {}})
        v["last_seen"] = time.time()
        return v["files"]

    def md5(self, vol: str, rel: str, path: Path, sig: tuple[int, int] | None) -> str:
        if sig is None:
            raise FileNotFoundError(path)
        with self._lock:
            hit = self._files(vol).get(rel)
        if not self.rehash and hit and hit[0] == sig[0] and hit[1] == sig[1]:
            self.hits += 1
            return hit[2]
        md5 = _md5(path, 8 * 1024 * 1024)
        with self._lock:
            self._files(vol)[rel] = [sig[0], sig[1], md5]
        return md5

    def forget(self, vol: str, rel: str) -> None:
        with self._lock:
            self._files(vol).pop(rel, None)

    def prune(self, vol: str, present: set[str]) -> None:
        """Drop entries for files no longer on this card, and cards not seen for HASH_CACHE_DAYS."""
        with self._lock:
            files = self._files(vol)
            for rel in [r for r in files if r not in present]:
                del files[rel]
            cutoff = time.time() - HASH_CACHE_DAYS * 86400
            for v in [v for v, d in self.data["volumes"].items() if d.get("last_seen", 0) < cutoff]:
                del self.data["volumes"][v]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)


def _sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _md5(path: Path, buf: int) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while chunk := fh.read(buf):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Item:
    card: str
    rel: str
    size: int
    md5: str = ""
    status: str = "pending"      # pending | skipped-existing | uploaded | failed | not-verified
    drive_id: str = ""
    error: str = ""


@dataclass
class Report:
    root_folder_id: str
    items: list[Item]
    seconds: float
    log_path: str
    unverified: list[Item] = field(default_factory=list)   # card files this run skipped or could not read

    @property
    def verified(self) -> list[Item]:
        return [i for i in self.items if i.status in ("uploaded", "skipped-existing")]

    @property
    def failed(self) -> list[Item]:
        return [i for i in self.items if i.status == "failed"]

    @property
    def safe_to_format(self) -> bool:
        return (bool(self.items) and not self.failed and not self.unverified
                and len(self.verified) == len(self.items))

    def summary(self) -> dict:
        up = [i for i in self.items if i.status == "uploaded"]
        return {
            "files": len(self.items),
            "uploaded": len(up),
            "already_on_drive": len([i for i in self.items if i.status == "skipped-existing"]),
            "failed": len(self.failed),
            "bytes_uploaded": sum(i.size for i in up),
            "bytes_total": sum(i.size for i in self.items),
            "seconds": round(self.seconds, 1),
            "drive_folder": f"https://drive.google.com/drive/folders/{self.root_folder_id}",
            "safe_to_format_card": self.safe_to_format,
            "failures": [{"file": f"{i.card}/{i.rel}", "error": i.error} for i in self.failed],
            "not_verified": len(self.unverified),
            "not_verified_files": [{"file": f"{i.card}/{i.rel}", "reason": i.error} for i in self.unverified[:50]],
            "log": self.log_path,
        }


def offload(cards: list[Path] | None = None, root_name: str = "A7V Offload", jobs: int = 3,
            dry_run: bool = False, log: Callable[[str], None] = print, scope: str = "camera",
            organize: bool = False, rehash: bool = False) -> Report:
    """organize=True → each file straight into the `drive organize` year/type layout (organize.classify).
    Otherwise scope='camera' → <root>/<capture date>/<card>/<path>; scope='other'|'all' → <root>/<card>-<today>/<path>."""
    t0 = time.time()
    cards = cards if cards is not None else find_cards()
    if not cards:
        raise FileNotFoundError(
            "No Sony card mounted. Either put the card in a reader, or on the camera set "
            "Menu → Network → Transfer/Remote → PC Remote Function → PC Remote: Off, then "
            "Setup → USB → USB Connection Mode → Mass Storage (MSC), and reconnect USB-C.")

    drive = Drive()
    root_id = drive.find_folder(root_name) if dry_run else drive.ensure_folder(root_name)   # dry run: no writes
    log(f"Drive auth: {drive.mode}; root folder '{root_name}' ({root_id or 'not created yet'})")

    items: list[tuple[Item, Path]] = []
    unverified: list[Item] = []
    vols: dict[str, str] = {}                    # card.name → volume id (hash cache key)
    for card in cards:
        files, skipped = scan(card, scope)
        vols[card.name] = volume_id(card)
        unverified += [_unverified(card, rel, why) for rel, why in skipped]
        items += [(Item(card=card.name, rel=str(p.relative_to(card)), size=(_sig(p) or (0,))[0]), p) for p in files]
    total = sum(i.size for i, _ in items)
    log(f"Found {len(items)} files, {total / 1e9:.2f} GB on {', '.join(c.name for c in cards)}"
        + (f"; {len(unverified)} other files NOT covered by this run" if unverified else ""))

    log("Indexing existing Drive files for dedup…")
    existing = drive.walk_md5(root_id) if root_id else {}
    log(f"  {len(existing)} files already under '{root_name}'")

    cache = HashCache(rehash=rehash)
    log("Hashing card files…" + (" (--rehash: reading every file)" if rehash else ""))
    sigs: dict[Path, tuple[int, int] | None] = {}
    for n, (item, path) in enumerate(items, 1):
        sigs[path] = _sig(path)                  # taken before reading: any later change → not verified
        try:
            item.md5 = cache.md5(vols[item.card], item.rel, path, sigs[path])
        except OSError as e:
            item.status, item.error = "failed", f"unreadable: {e}"[:300]
        if item.md5 in existing:
            item.status = "skipped-existing"
            item.drive_id = existing[item.md5]["id"]
        if n % 25 == 0 or n == len(items):
            log(f"  hashed {n}/{len(items)} ({cache.hits} unchanged, from cache)")
        if n % 200 == 0:
            cache.save()                         # an interrupted run keeps what it already read

    todo = [(i, p) for i, p in items if i.status == "pending"]
    log(f"{len(todo)} to upload ({sum(i.size for i, _ in todo) / 1e9:.2f} GB), "
        f"{len(items) - len(todo)} already on Drive or unreadable")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"offload-{dt.datetime.now():%Y%m%d-%H%M%S-%f}.jsonl"
    logged: dict[int, str] = {}                  # id(item) → status last written to the JSONL log

    if not dry_run and todo:
        _upload_all(drive, todo, root_id, scope, jobs, log, log_path, logged, organize)

    _recheck(items, sigs, cards, scope, unverified)
    for item, path in items:
        if item.status == "failed" and "changed on the card" in item.error:
            cache.forget(vols[item.card], item.rel)
    if scope == "all":                           # only a full scan knows which files are gone
        for card in cards:
            cache.prune(vols[card.name], {i.rel for i, _ in items if i.card == card.name})
    cache.save()
    with log_path.open("a") as fh:
        for i, _ in items:
            if logged.get(id(i)) != i.status:
                fh.write(json.dumps(asdict(i)) + "\n")
        fh.writelines(json.dumps(asdict(i)) + "\n" for i in unverified)
    return Report(root_id or "", [i for i, _ in items], time.time() - t0, str(log_path), unverified)


def _unverified(card: Path, rel: str, reason: str) -> Item:
    try:
        size = (card / rel).lstat().st_size
    except OSError:
        size = 0
    return Item(card=card.name, rel=rel, size=size, status="not-verified", error=reason)


def _recheck(items: list[tuple[Item, Path]], sigs: dict, cards: list[Path], scope: str,
             unverified: list[Item]) -> None:
    """A file that changed after it was hashed, or appeared during the run, is not verified."""
    for item, path in items:
        if item.status in ("uploaded", "skipped-existing") and _sig(path) != sigs.get(path):
            item.status, item.error = "failed", "changed on the card during offload; rerun"
    known = {(i.card, i.rel) for i, _ in items} | {(i.card, i.rel) for i in unverified}
    for card in cards:
        files, skipped = scan(card, scope)
        for rel, why in [(str(p.relative_to(card)), "appeared during offload; rerun") for p in files] + skipped:
            if (card.name, rel) not in known:
                unverified.append(_unverified(card, rel, why))


def _destination(item: Item, path: Path, scope: str, organize: bool) -> tuple[str, ...]:
    if organize:
        sig = _sig(path)
        return classify(item.rel, meta={"mtime": sig[1] / 1e9} if sig else None)
    if scope == "camera":
        day = dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
        return (day, item.card, *Path(item.rel).parent.parts)
    return (f"{item.card}-{dt.date.today().isoformat()}", *Path(item.rel).parent.parts)


def _upload_all(drive: Drive, todo: list[tuple[Item, Path]], root_id: str, scope: str, jobs: int,
                log: Callable[[str], None], log_path: Path, logged: dict[int, str], organize: bool = False) -> None:
    # Create destination folders (and pick clash-free names) serially: avoids duplicate-folder races between workers.
    folder_ids: dict[tuple[str, ...], str] = {}
    taken: dict[str, dict[str, str]] = {}        # organized layout is flat: folder id → casefold name → md5
    for item, path in todo:
        parts = _destination(item, path, scope, organize)
        if parts not in folder_ids:
            folder_ids[parts] = drive.ensure_path(list(parts), root_id)
        folder, name = folder_ids[parts], None
        if organize:
            if folder not in taken:
                taken[folder] = {f["name"].casefold(): f.get("md5Checksum", "") for f in drive.children(folder)}
            name = free_name(Path(item.rel).name, taken[folder])
            taken[folder][name.casefold()] = item.md5
        item._folder, item._name = folder, name  # type: ignore[attr-defined]

    lock = threading.Lock()
    local = threading.local()
    done_bytes = 0
    upload_total = sum(i.size for i, _ in todo)

    def worker(item: Item, path: Path) -> Item:
        nonlocal done_bytes
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            if not hasattr(local, "drive"):
                local.drive = Drive()
            name = {"name": item._name} if item._name else {}  # type: ignore[attr-defined]
            meta = local.drive.upload(path, item._folder, mime, **name)  # type: ignore[attr-defined]
            if meta.get("md5Checksum") != item.md5:
                item.status, item.error = "failed", f"md5 mismatch: local {item.md5} drive {meta.get('md5Checksum')}"
            else:
                item.status, item.drive_id = "uploaded", meta["id"]
        except Exception as e:  # noqa: BLE001 — record and continue with the rest of the card
            item.status, item.error = "failed", str(e)[:300]
        with lock:
            done_bytes += item.size
            with log_path.open("a") as fh:
                fh.write(json.dumps(asdict(item)) + "\n")
            logged[id(item)] = item.status
            pct = 100 * done_bytes / upload_total if upload_total else 100
            log(f"  [{pct:5.1f}%] {item.status:8} {item.card}/{item.rel} ({item.size / 1e6:.1f} MB)"
                + (f" — {item.error}" if item.error else ""))
        return item

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for f in as_completed([pool.submit(worker, i, p) for i, p in todo]):
            f.result()
