"""`camctl drive organize`: sort the card archive on Drive by year, then type.

  <root>/<YYYY>/Photos/              phone and other photos (flat)
  <root>/<YYYY>/Videos/              phone and other videos (flat)
  <root>/<YYYY>/Camera/RAW|JPEG/     stills from a camera's DCIM/<3 digits><5 chars>/ folder (flat)
  <root>/<YYYY>/Camera/Videos/       PRIVATE/M4ROOT/CLIP|SUB clips with their XML sidecars (flat)
  <root>/Documents/…                 the card's own top-level folders listed in keep_folders, subpaths kept
                                     (these win over the type rules)
  <root>/Music/…                     the card's music_sources folders, plus audio found elsewhere (subpaths kept)
  <root>/Other/<card path>           everything else

The folder names come from ~/.config/camctl/organize.yaml (or $CAMCTL_ORGANIZE_CONFIG), see `load_layout`:

  keep_folders: [Documents, Projects]  # card top-level folders kept whole (matched case-insensitively)
  music_folder: Music                # archive folder for audio
  music_sources: [Music, Audiobooks]   # card folders whose whole contents go to music_folder

Without the file: keep_folders [Documents], music_folder Music, music_sources [Music]. Changing these on an
existing archive changes what counts as organized, so check with a dry run before --apply.

Files are found in the layouts camctl offload has written: `<card>-<upload date>/<card path>` (scope other/all,
scripts/follow_upload.py) and `<capture date>/<card>/<card path>` (scope camera). Files already in the layout
above are left alone, so a second run makes no moves.

Only metadata changes: a move is files.update addParents/removeParents (content and md5 untouched), duplicates
(same md5) and emptied folders go to Drive trash (recoverable for 30 days). Nothing is ever deleted permanently.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

import yaml

from . import drive as drive_mod
from .paths import CONFIG_DIR

LOG_DIR = CONFIG_DIR / "offload-logs"
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".dng", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".3gp", ".mkv", ".avi", ".m4v", ".webm", ".mts", ".m2ts", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}
RAW_EXT = {".arw", ".srf", ".sr2", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".dng", ".pef",
           ".srw", ".x3f", ".3fr", ".iiq"}
CAMERA_JPEG_EXT = {".jpg", ".jpeg", ".hif", ".heif", ".heic"}

OTHER = "Other"
UNKNOWN_YEAR = "Unknown year"
TYPE_LEAVES = {("Photos",), ("Videos",), ("Camera", "RAW"), ("Camera", "JPEG"), ("Camera", "Videos")}
# Media under these card folders is app data / recovered fragments / camera databases, not the user's photos.
NO_MEDIA_TOPS = {"android", "lost.dir", "avf_info", "rw_lib", "private"}
CAMERA_DIR = re.compile(r"^\d{3}[A-Za-z0-9_]{5}$")      # DCIM/100MSDCF, 101_PANA, 100CANON, 100_FUJI …
PHONE_DCIM_SUFFIX = {"ANDRO", "APPLE", "CLOUD"}          # …but DCIM/100ANDRO, 100APPLE are phones

YEAR_DIR = re.compile(r"^(?:19|20)\d{2}$")
CAPTURE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")                     # offload scope=camera: <date>/<card>/…
UPLOAD_DIR = re.compile(r"^(?P<card>.+)-(?P<date>\d{4}-\d{2}-\d{2})$")  # offload scope=other|all: <card>-<date>/…
EXIF_TIME = re.compile(r"^\s*(\d{4})[:\-]")
NAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})([-_.]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])(?!\d)")
SONY_SIBLING = re.compile(r"^(C\d{4})[MS]\d{2}$", re.I)   # C0001M01.XML sidecar / C0001S03.MP4 proxy → C0001
XML_CREATION = re.compile(rb'CreationDate\s+value="(\d{4})-')
OFFLOAD_CMD = re.compile(r"\b(?:camctl|a7ctl)(?:\s+\S+)*?\s+offload\b(?!-)|follow_upload\.py")


class OrganizeError(RuntimeError):
    pass


# -- layout config -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """The user-named parts of the archive layout (see the module docstring)."""
    keep: dict[str, str]                  # casefolded card top-level folder → archive folder name
    music: str                            # archive folder for audio
    music_sources: frozenset[str]         # casefolded card top-level folders whose contents all go to music

    @property
    def named_tops(self) -> tuple[str, ...]:
        return (*self.keep.values(), self.music)


DEFAULT_LAYOUT = Layout({"documents": "Documents"}, "Music", frozenset({"music"}))
_layout_cache: Layout | None = None


def layout_path() -> Path:
    return Path(os.environ.get("CAMCTL_ORGANIZE_CONFIG") or CONFIG_DIR / "organize.yaml").expanduser()


def _folder_name(value: object, what: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value:
        raise OrganizeError(f"{path}: {what} must be a folder name (non-empty, no '/'), got {value!r}")
    return value.strip()


def load_layout(path: Path | None = None) -> Layout:
    """Layout from organize.yaml; DEFAULT_LAYOUT when the file doesn't exist. A malformed file raises instead of
    falling back, because a different layout would move an existing archive's folders."""
    path = path or layout_path()
    if not path.exists():
        return DEFAULT_LAYOUT
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise OrganizeError(f"Cannot read {path}: {e}") from e
    allowed = {"keep_folders", "music_folder", "music_sources"}
    if not isinstance(data, dict) or set(data) - allowed:
        extra = sorted(set(data) - allowed) if isinstance(data, dict) else data
        raise OrganizeError(f"{path}: expected only {sorted(allowed)}, got {extra!r}")

    def names(key: str, default: list[str]) -> list[str]:
        value = data.get(key, default)
        if not isinstance(value, list):
            raise OrganizeError(f"{path}: {key} must be a list of folder names")
        return [_folder_name(v, key, path) for v in value]

    keep = names("keep_folders", ["Documents"])
    music = _folder_name(data.get("music_folder", DEFAULT_LAYOUT.music), "music_folder", path)
    sources = names("music_sources", [music])
    return Layout({k.casefold(): k for k in keep}, music, frozenset(s.casefold() for s in sources))


def layout() -> Layout:
    global _layout_cache
    if _layout_cache is None:
        _layout_cache = load_layout()
    return _layout_cache


# -- classifier (pure) ---------------------------------------------------------------------------

def _valid_year(y: int) -> bool:
    return 1995 <= y <= dt.date.today().year


def _iso(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def year_source(name: str, meta: dict | None = None) -> tuple[int | None, str]:
    """(year, where it came from) or (None, "unknown"). Order: EXIF (Drive imageMediaMetadata.time) → date in the
    file name → Sony XML sidecar CreationDate → offload capture-date folder → local mtime (offload) → Drive
    modifiedTime when it was preserved from the local file (clearly older than createdTime, the upload time) →
    a sibling's year (DSC0001.ARW ↔ .JPG, C0001.MP4 ↔ C0001M01.XML). Years before 1995 or in the future are
    ignored (unset camera clocks)."""
    meta = meta or {}
    m = EXIF_TIME.match(str((meta.get("imageMediaMetadata") or {}).get("time") or ""))
    if m and _valid_year(int(m.group(1))):
        return int(m.group(1)), "exif"
    for m in NAME_DATE.finditer(name):
        if _valid_year(int(m.group(1))):
            return int(m.group(1)), "filename"
    for key, source in (("sidecar_time", "sidecar"), ("capture_date", "capture-date")):
        m = re.match(r"^(\d{4})", str(meta.get(key) or ""))
        if m and _valid_year(int(m.group(1))):
            return int(m.group(1)), source
    if meta.get("mtime") is not None:
        y = dt.datetime.fromtimestamp(float(meta["mtime"])).year
        if _valid_year(y):
            return y, "mtime"
    modified, created = _iso(meta.get("modifiedTime") or ""), _iso(meta.get("createdTime") or "")
    if modified and created and created - modified > dt.timedelta(days=1) and _valid_year(modified.year):
        return modified.year, "modified-time"
    if meta.get("sibling_year") and _valid_year(int(meta["sibling_year"])):
        return int(meta["sibling_year"]), "sibling"
    return None, "unknown"


def _camera_kind(dirs: tuple[str, ...], ext: str) -> str | None:
    up = tuple(d.upper() for d in dirs)
    if (len(up) == 2 and up[0] == "DCIM" and CAMERA_DIR.match(dirs[1]) and up[1][3:] not in PHONE_DCIM_SUFFIX):
        if ext in RAW_EXT:
            return "RAW"
        if ext in CAMERA_JPEG_EXT:
            return "JPEG"
        if ext in VIDEO_EXT:
            return "Videos"
    if len(up) == 3 and up[:2] == ("PRIVATE", "M4ROOT") and up[2] in ("CLIP", "SUB") and ext in VIDEO_EXT | {".xml"}:
        return "Videos"
    if up[:4] == ("PRIVATE", "AVCHD", "BDMV", "STREAM") and ext in (".mts", ".m2ts"):
        return "Videos"
    return None


def classify(rel_path: str, name: str | None = None, md5: str = "", meta: dict | None = None) -> tuple[str, ...]:
    """Destination folder (parts under the archive root) for a file at card-relative rel_path.

    name defaults to the last component of rel_path; md5 is accepted for callers that key on it (placement never
    depends on content); meta: Drive file fields and/or capture_date, mtime, sidecar_time, sibling_year."""
    rel = PurePosixPath(rel_path)
    name = name or rel.name
    dirs = rel.parts[:-1]
    ext = PurePosixPath(name).suffix.lower()
    top = dirs[0].casefold() if dirs else ""
    lay = layout()
    if top in lay.keep:
        return (lay.keep[top], *dirs[1:])
    if top in lay.music_sources:
        return (lay.music, *dirs[1:])
    kind = _camera_kind(dirs, ext)
    if kind:
        return (_year_dir(name, meta), "Camera", kind)
    if top in NO_MEDIA_TOPS or any(d.startswith(".") for d in dirs):
        return (OTHER, *dirs)
    if ext in AUDIO_EXT:
        return (lay.music, *dirs)
    if ext in PHOTO_EXT:
        return (_year_dir(name, meta), "Photos")
    if ext in VIDEO_EXT:
        return (_year_dir(name, meta), "Videos")
    return (OTHER, *dirs)


def _year_dir(name: str, meta: dict | None) -> str:
    y, _ = year_source(name, meta)
    return str(y) if y else UNKNOWN_YEAR


def _organized_top(name: str) -> bool:
    return name in (*layout().named_tops, OTHER, UNKNOWN_YEAR) or bool(YEAR_DIR.match(name))


def is_organized(parts: tuple[str, ...]) -> bool:
    """True if a folder path (under the archive root) is already in the organized layout."""
    if not parts:
        return False
    if parts[0] in (*layout().named_tops, OTHER):
        return True
    return (bool(YEAR_DIR.match(parts[0])) or parts[0] == UNKNOWN_YEAR) and tuple(parts[1:]) in TYPE_LEAVES


def locate(parts: tuple[str, ...], name: str) -> tuple[str, str, str | None]:
    """(layout, card-relative path, capture date) for a file at folder path `parts` under a managed root.
    layout: organized (rel "") | dated (<date>/<card>/<rel>) | card-copy (<card>-<date>/<rel>) | unrecognized."""
    if is_organized(parts):
        return "organized", "", None
    if len(parts) >= 2 and CAPTURE_DIR.match(parts[0]):
        return "dated", "/".join((*parts[2:], name)), parts[0]
    if parts and UPLOAD_DIR.match(parts[0]):
        return "card-copy", "/".join((*parts[1:], name)), None
    return "unrecognized", "/".join((*parts, name)), None


def free_name(name: str, taken: dict[str, str], prefer: int = 1) -> str:
    """name, or "stem (n).ext" if a different file already holds it in the folder (taken: casefold name → md5).
    prefer: try this suffix number first (keeps C0001 (2).MP4 and C0001M01 (2).XML together)."""
    p = PurePosixPath(name)
    stem, ext = p.stem, p.suffix

    def cand(n: int) -> str:
        return name if n == 1 else f"{stem} ({n}){ext}"
    if cand(prefer).casefold() not in taken:
        return cand(prefer)
    n = 1
    while cand(n).casefold() in taken:
        n += 1
    return cand(n)


def _suffix_no(orig: str, final: str) -> int:
    p = PurePosixPath(orig)
    m = re.fullmatch(re.escape(p.stem) + r" \((\d+)\)" + re.escape(p.suffix), final)
    return int(m.group(1)) if m else 1


def _sibling_key(name: str) -> str:
    stem = PurePosixPath(name).stem
    m = SONY_SIBLING.match(stem)
    return (m.group(1) if m else stem).upper()


# -- plan --------------------------------------------------------------------------------------------

ROOT_AREA = ""   # Entry.area of files under the archive root itself


@dataclass
class Entry:
    id: str
    name: str
    md5: str
    size: int
    parent: str                  # folder id it is in now
    area: str                    # managed root it was found under (the archive root or a merge folder)
    path: tuple[str, ...]        # folder names from that root down to the parent
    created: str
    meta: dict
    layout: str = ""
    rel: str = ""
    dest: tuple[str, ...] = ()
    dest_name: str = ""
    year_source: str = ""

    @property
    def in_place(self) -> bool:
        return self.dest == self.path and self.dest_name == self.name and self.area == ROOT_AREA

    def where(self, root_name: str) -> str:
        return "/".join((root_name if self.area == ROOT_AREA else self.area, *self.path, self.name))


@dataclass
class Plan:
    root: str
    root_id: str
    merge: dict[str, str]                                  # merge folder name → id (found ones)
    entries: list[Entry]
    keep: list[Entry]
    trash: list[tuple[Entry, Entry]]                       # (duplicate, the copy that is kept)
    skipped: list[dict]                                    # files without md5 (Google Docs…): not touched
    folders: dict[str, dict[str, dict]]                    # area → folder id → {id, name, parent, path}
    missing_merge: list[str] = field(default_factory=list)

    @property
    def moves(self) -> list[Entry]:
        return [e for e in self.keep if not e.in_place]

    def empty_after(self) -> list[str]:
        """Folder paths that will hold no file once the plan is applied (dry-run estimate)."""
        staying: set[tuple[str, tuple[str, ...]]] = set()
        for e in [*(e for e in self.keep if e.in_place), *self.skipped_entries()]:
            staying |= {(e.area, e.path[:i]) for i in range(len(e.path) + 1)}
        out = []
        for area, folders in self.folders.items():
            if area != ROOT_AREA and (area, ()) not in staying:
                out.append(area)                  # the whole merge folder goes; its subfolders with it
                continue
            gone = {f["path"] for f in folders.values() if (area, f["path"]) not in staying
                    and not (area == ROOT_AREA and _organized_top(f["path"][0]))}
            out += ["/".join((area or self.root, *p)) for p in sorted(gone) if p[:-1] not in gone]
        return sorted(out)

    def skipped_entries(self) -> list[Entry]:
        return [Entry(id=f["id"], name=f["name"], md5="", size=0, parent="", area=f["_area"], path=f["_path"],
                      created="", meta={}) for f in self.skipped]

    def summary(self, samples: int = 25) -> dict:
        dests = Counter(_dest_key(e.dest) for e in self.keep)
        dup_bytes = sum(d.size for d, _ in self.trash)
        moves = self.moves
        return {
            "root": self.root,
            "drive_folder": f"https://drive.google.com/drive/folders/{self.root_id}",
            "merge": sorted(self.merge),
            "merge_not_found": self.missing_merge,
            "files": len(self.entries),
            "files_by_area": dict(Counter(e.area or self.root for e in self.entries)),
            "layouts": dict(Counter(e.layout for e in self.entries)),
            "destinations": dict(sorted(dests.items())),
            "years": dict(sorted(Counter(e.dest[0] for e in self.keep if _is_year_dest(e.dest)).items())),
            "year_sources": dict(Counter(e.year_source for e in self.keep if _is_year_dest(e.dest))),
            "unknown_year": sum(1 for e in self.keep if e.dest and e.dest[0] == UNKNOWN_YEAR),
            "already_in_place": len(self.keep) - len(moves),
            "moves": len(moves),
            "renamed_for_name_clash": sum(1 for e in moves if e.dest_name != e.name),
            "duplicates_to_trash": len(self.trash),
            "duplicate_bytes": dup_bytes,
            "not_touched_no_md5": len(self.skipped),
            "folders_emptied_to_trash": len(self.empty_after()),
            "sample_moves": [{"from": e.where(self.root), "to": "/".join((self.root, *e.dest, e.dest_name))}
                             for e in moves[:samples]],
            "sample_duplicates": [{"trash": d.where(self.root), "keep": k.where(self.root)}
                                  for d, k in self.trash[:samples]],
        }


def _is_year_dest(dest: tuple[str, ...]) -> bool:
    return bool(dest) and (bool(YEAR_DIR.match(dest[0])) or dest[0] == UNKNOWN_YEAR)


def _dest_key(dest: tuple[str, ...]) -> str:
    return "/".join(dest) if _is_year_dest(dest) else dest[0]


def _keep_rank(e: Entry) -> tuple:
    """Lower wins among copies with the same md5: already in place, then a user-named folder, then year/type,
    then Other; ties → oldest upload, then id."""
    top = e.dest[0] if e.dest else OTHER
    lay = layout()
    prio = 0 if top in lay.keep.values() else 1 if top == lay.music else 3 if top == OTHER else 2
    return (not (e.area == ROOT_AREA and e.layout == "organized"), prio, e.created or "~", e.id)


def _walk(drive, areas: dict[str, str]) -> tuple[list[dict], dict[str, dict[str, dict]]]:
    files: list[dict] = []
    folders: dict[str, dict[str, dict]] = {}
    seen: set[str] = set()
    for area, fid in areas.items():
        fs, folders[area] = drive.walk_tree(fid)
        for f in fs:
            if f["id"] in seen:                   # a file with two parents is listed twice: it is ONE file
                continue
            seen.add(f["id"])
            files.append({**f, "_area": area})
    return files, folders


def build_plan(drive, root_name: str, merge: tuple[str, ...] | list[str] = (),
               log: Callable[[str], None] = lambda m: None) -> Plan:
    root_id = drive.find_folder(root_name)
    if not root_id:
        raise OrganizeError(f"no Drive folder named {root_name!r} in My Drive")
    merge_ids = {m: drive.find_folder(m) for m in merge if m and m != root_name}
    missing = sorted(m for m, i in merge_ids.items() if not i)
    merge_ids = {m: i for m, i in merge_ids.items() if i}
    log(f"Walking '{root_name}'" + (f" + {', '.join(merge_ids)}" if merge_ids else "") + "…")
    files, folders = _walk(drive, {ROOT_AREA: root_id, **merge_ids})
    entries, skipped = [], []
    for f in files:
        if not f.get("md5Checksum"):
            skipped.append(f)
            continue
        e = Entry(id=f["id"], name=f["name"], md5=f["md5Checksum"], size=int(f.get("size") or 0),
                  parent=f["_parent"], area=f["_area"], path=tuple(f["_path"]), created=f.get("createdTime", ""),
                  meta={k: f[k] for k in ("imageMediaMetadata", "modifiedTime", "createdTime") if k in f})
        e.layout, e.rel, capture = locate(e.path, e.name)
        if capture:
            e.meta["capture_date"] = capture
        entries.append(e)
    log(f"  {len(entries)} files ({len(skipped)} without md5 left alone)")
    _sidecar_years(drive, entries, log)
    _sibling_years(entries)
    for e in entries:
        if e.layout == "organized":
            e.dest, e.dest_name = e.path, e.name
            e.year_source = "organized"
        else:
            e.dest = classify(e.rel, e.name, e.md5, e.meta)
            e.year_source = year_source(e.name, e.meta)[1] if _is_year_dest(e.dest) else ""
    keep, trash = _dedup(entries)
    _assign_names(keep)
    return Plan(root_name, root_id, merge_ids, entries, keep, trash, skipped, folders, missing)


def _sidecar_years(drive, entries: list[Entry], log: Callable[[str], None]) -> None:
    """Sony clips carry no date Drive can read; their XML sidecar has <CreationDate value="YYYY-…">."""
    todo = [e for e in entries if e.layout != "organized" and e.name.lower().endswith(".xml")
            and re.search(r"(^|/)PRIVATE/M4ROOT/(CLIP|SUB)/[^/]+$", e.rel, re.I)
            and year_source(e.name, e.meta)[0] is None]
    if todo:
        log(f"  reading {len(todo)} clip XML sidecars for recording dates…")
    for e in todo:
        try:
            m = XML_CREATION.search(drive.read_head(e.id))
        except Exception:  # noqa: BLE001 — a sidecar we cannot read just leaves the year unknown
            continue
        if m:
            e.meta["sidecar_time"] = m.group(1).decode()


def _sibling_years(entries: list[Entry]) -> None:
    groups: dict[tuple, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.layout != "organized":
            groups[(e.area, e.path, _sibling_key(e.name))].append(e)
    for members in groups.values():
        years = sorted(y for y in (year_source(e.name, e.meta)[0] for e in members) if y)
        if years:
            for e in members:
                e.meta.setdefault("sibling_year", Counter(years).most_common(1)[0][0])


def _dedup(entries: list[Entry]) -> tuple[list[Entry], list[tuple[Entry, Entry]]]:
    """Same md5 → keep one. Zero-byte files are only duplicates when they would also land on the same path."""
    groups: dict[tuple, list[Entry]] = defaultdict(list)
    for e in entries:
        key = (e.md5,) if e.md5 != EMPTY_MD5 else (e.md5, e.dest, e.dest_name or e.name)
        groups[key].append(e)
    keep, trash = [], []
    for members in groups.values():
        members.sort(key=_keep_rank)
        keep.append(members[0])
        trash += [(d, members[0]) for d in members[1:] if d.id != members[0].id]
    trash.sort(key=lambda t: (t[0].area, t[0].path, t[0].name))
    return keep, trash


def _assign_names(keep: list[Entry]) -> None:
    taken: dict[tuple[str, ...], dict[str, str]] = defaultdict(dict)
    for e in keep:
        if e.area == ROOT_AREA and e.layout == "organized":
            taken[e.dest][e.name.casefold()] = e.md5
    group_no: dict[tuple, int] = {}
    for e in sorted((e for e in keep if not (e.area == ROOT_AREA and e.layout == "organized")),
                    key=lambda e: (e.created or "~", e.id)):
        gkey = (e.area, e.path, _sibling_key(e.name))
        e.dest_name = free_name(e.name, taken[e.dest], prefer=group_no.get(gkey, 1))
        group_no.setdefault(gkey, _suffix_no(e.name, e.dest_name))
        taken[e.dest][e.dest_name.casefold()] = e.md5


# -- apply ---------------------------------------------------------------------------------------------

def offload_running() -> list[str]:
    """Command lines of camctl offload / follow_upload processes (other than this one)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    me = str(os.getpid())
    return [line.strip()[:200] for line in out.splitlines()
            if OFFLOAD_CMD.search(line) and line.split(None, 1)[:1] != [me]]


@dataclass
class Result:
    plan: dict
    applied: bool
    log_path: str = ""
    moved: int = 0
    trashed_duplicates: int = 0
    trashed_folders: int = 0
    failures: list[dict] = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failures and self.verification.get("ok", True)

    def summary(self) -> dict:
        out = {"dry_run": not self.applied, **self.plan}
        if self.applied:
            out.update(moved=self.moved, trashed_duplicates=self.trashed_duplicates,
                       trashed_empty_folders=self.trashed_folders, failed=len(self.failures),
                       failures=self.failures[:50], verification=self.verification)
        out.update(ok=self.ok, seconds=round(self.seconds, 1), log=self.log_path)
        return out


def organize(root_name: str = "Card Archive", merge: tuple[str, ...] | list[str] = ("A7V Offload",),
             dry_run: bool = True, jobs: int = 8, log: Callable[[str], None] = print,
             allow_running_offload: bool = False) -> Result:
    """Plan (dry_run) or apply the year/type layout. Re-running continues an interrupted apply."""
    t0 = time.time()
    drive = drive_mod.Drive()
    plan = build_plan(drive, root_name, merge, log)
    summary = plan.summary()
    if dry_run:
        return Result(summary, False, seconds=time.time() - t0)
    busy = [] if allow_running_offload else offload_running()
    if busy:
        raise OrganizeError("an offload is still running — wait for it to finish (organize trashes emptied folders "
                            "it may still upload into): " + " | ".join(busy[:3]))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"organize-{dt.datetime.now():%Y%m%d-%H%M%S-%f}.jsonl"
    res = Result(summary, True, str(log_path))
    lock = threading.Lock()

    def record(op: str, status: str, **kw) -> None:
        with lock:
            with log_path.open("a") as fh:
                fh.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"), "op": op,
                                     "status": status, **kw}, ensure_ascii=False) + "\n")
            if status == "failed" and op != "verify":      # verification has its own section
                res.failures.append({"op": op, **kw})

    record("plan", "ok", **{k: v for k, v in summary.items() if not k.startswith("sample_")})
    before = Counter(e.md5 for e in plan.entries)

    # Destination folders first, serially (parallel ensure_folder calls would race into duplicate folders).
    folder_ids = {f["path"]: fid for fid, f in plan.folders[ROOT_AREA].items()}
    folder_ids[()] = plan.root_id
    for e in plan.moves:
        for i in range(1, len(e.dest) + 1):
            if e.dest[:i] not in folder_ids:
                folder_ids[e.dest[:i]] = drive.ensure_folder(e.dest[i - 1], folder_ids[e.dest[:i - 1]])
                record("mkdir", "ok", path="/".join((root_name, *e.dest[:i])), id=folder_ids[e.dest[:i]])

    local = threading.local()

    def worker_drive():
        if not hasattr(local, "drive"):
            local.drive = drive_mod.Drive()
        return local.drive

    def trash_dup(dup: Entry, kept: Entry) -> None:
        info = {"id": dup.id, "md5": dup.md5, "path": dup.where(root_name), "kept": kept.where(root_name),
                "kept_id": kept.id}
        try:
            worker_drive().trash(dup.id)
        except Exception as ex:  # noqa: BLE001 — logged; a re-run retries it
            record("trash-duplicate", "failed", **info, error=str(ex)[:300])
            return
        record("trash-duplicate", "ok", **info)
        with lock:
            res.trashed_duplicates += 1

    def move(e: Entry) -> None:
        to = folder_ids[e.dest]
        info = {"id": e.id, "md5": e.md5, "from": e.where(root_name), "to": "/".join((root_name, *e.dest,
                                                                                         e.dest_name))}
        try:
            worker_drive().update(e.id, add_parent=to if to != e.parent else None,
                                  remove_parent=e.parent if to != e.parent else None,
                                  name=e.dest_name if e.dest_name != e.name else None)
        except Exception as ex:  # noqa: BLE001
            record("move", "failed", **info, error=str(ex)[:300])
            return
        record("move", "ok", **info)
        with lock:
            res.moved += 1

    for label, tasks in (("duplicates to trash", [(trash_dup, d, k) for d, k in plan.trash]),
                         ("moves", [(move, e) for e in plan.moves])):
        if not tasks:
            continue
        log(f"Applying {len(tasks)} {label}…")
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for n, fut in enumerate(as_completed([pool.submit(t[0], *t[1:]) for t in tasks]), 1):
                fut.result()
                if n % 100 == 0 or n == len(tasks):
                    log(f"  {n}/{len(tasks)}")

    log("Verifying…")
    areas = {ROOT_AREA: plan.root_id, **plan.merge}
    files, folders = _walk(drive, areas)
    res.verification = _verify(before, files)
    record("verify", "ok" if res.verification["ok"] else "failed", **res.verification)
    if res.verification["ok"] and not res.failures:
        for area, fid in areas.items():
            for folder_id, path in _empty_folders(files, folders[area], area, fid):
                try:
                    drive.trash(folder_id)
                except Exception as ex:  # noqa: BLE001
                    record("trash-folder", "failed", id=folder_id, path=path, error=str(ex)[:300])
                    continue
                record("trash-folder", "ok", id=folder_id, path=path)
                res.trashed_folders += 1
    res.seconds = time.time() - t0
    return res


def _verify(before: Counter, files: list[dict]) -> dict:
    """Every md5 seen before must now be under the root exactly once (zero-byte files: at least once)."""
    in_root = Counter(f["md5Checksum"] for f in files if f.get("md5Checksum") and f["_area"] == ROOT_AREA)
    elsewhere = Counter(f["md5Checksum"] for f in files if f.get("md5Checksum") and f["_area"] != ROOT_AREA)
    missing = sorted(m for m in before if not in_root[m] and not elsewhere[m])
    not_moved = sorted(m for m in before if not in_root[m] and elsewhere[m])
    duplicated = sorted(m for m in before if in_root[m] > 1 and m != EMPTY_MD5)
    return {"ok": not (missing or not_moved or duplicated), "md5s_checked": len(before),
            "missing": missing[:100], "missing_count": len(missing),
            "still_in_merge_folder": not_moved[:100], "still_in_merge_folder_count": len(not_moved),
            "duplicated": duplicated[:100], "duplicated_count": len(duplicated)}


def _empty_folders(files: list[dict], folders: dict[str, dict], area: str, top_id: str) -> list[tuple[str, str]]:
    """(id, path) of the topmost folders under top_id with no file anywhere below them. A merge folder that holds
    no file at all is returned itself; the archive root and its organized folders never are."""
    full = set()
    for f in files:
        if f["_area"] != area:
            continue
        fid = f["_parent"]
        while fid in folders and fid not in full:
            full.add(fid)
            fid = folders[fid]["parent"]
    label = area or "root"
    if area != ROOT_AREA and not any(f["_area"] == area for f in files):
        return [(top_id, area)]
    # Under the archive root only the old offload folders are cleaned up, never the organized tree: a listing that
    # lags behind a just-finished move must not make a destination folder look empty.
    empty = {fid for fid in folders if fid not in full
             and not (area == ROOT_AREA and _organized_top(folders[fid]["path"][0]))}
    return sorted(((fid, "/".join((label, *folders[fid]["path"]))) for fid in empty
                   if folders[fid]["parent"] not in empty), key=lambda t: t[1])
