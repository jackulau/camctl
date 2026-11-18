"""Minimal Google Drive v3 client: OAuth (installed-app loopback), resumable upload, folder helpers.

Credentials lookup (first hit wins):
  token:        $CAMCTL_DRIVE_TOKEN, ~/.config/camctl/drive-token.json, ~/drive-uploader/token.json
  client JSON:  $CAMCTL_DRIVE_CREDENTIALS, ~/.config/camctl/drive-credentials.json, ~/drive-uploader/credentials.json
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import requests

from .paths import CONFIG_DIR

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 64 * 1024 * 1024  # must be a multiple of 256 KiB
LIST_FIELDS = "id,name,mimeType,size,md5Checksum"
# Everything `camctl drive organize` needs to place a file (imageMediaMetadata.time = EXIF DateTimeOriginal).
WALK_FIELDS = "id,name,mimeType,size,md5Checksum,parents,modifiedTime,createdTime,imageMediaMetadata(time)"
RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


def _first_existing(env: str, *paths: Path) -> Path:
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    for p in paths:
        if p.exists():
            return p
    return paths[0]


def token_path() -> Path:
    return _first_existing("CAMCTL_DRIVE_TOKEN", CONFIG_DIR / "drive-token.json",
                           Path.home() / "drive-uploader" / "token.json")


def credentials_path() -> Path:
    return _first_existing("CAMCTL_DRIVE_CREDENTIALS", CONFIG_DIR / "drive-credentials.json",
                           Path.home() / "drive-uploader" / "credentials.json")


class DriveError(RuntimeError):
    pass


def _client() -> dict:
    path = credentials_path()
    if not path.exists():
        raise DriveError(f"No OAuth client JSON at {path}. Create a Desktop OAuth client with the Drive API "
                         "enabled and save it there (or set CAMCTL_DRIVE_CREDENTIALS).")
    data = json.loads(path.read_text())
    return data.get("installed") or data.get("web") or data


def login() -> dict:
    """Run the loopback OAuth flow in a browser and persist the token."""
    client = _client()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"camctl: Drive login complete, you can close this tab.")

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })
    print(f"Opening browser for Google login:\n{url}")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        srv.handle_request()
    if result.get("state") != state or "code" not in result:
        raise DriveError(f"login failed: {result.get('error', 'state mismatch')}")
    r = requests.post(TOKEN_URL, data={
        "code": result["code"], "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    tok["expiry"] = time.time() + tok.get("expires_in", 3600) - 60
    out = CONFIG_DIR / "drive-token.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tok, indent=2))
    out.chmod(0o600)
    return tok


def _gcloud_token() -> dict | None:
    """{access_token, expiry} from the active gcloud account, if it was granted Drive scope
    (`gcloud auth login --enable-gdrive-access`). gcloud hands back its cached token, which may be close to
    expiry, so the real lifetime comes from tokeninfo."""
    if not shutil.which("gcloud"):
        return None
    try:
        out = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    tok = out.stdout.strip()
    if out.returncode != 0 or not tok:
        return None
    info = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": tok}, timeout=15)
    if not info.ok or SCOPE not in info.json().get("scope", "").split():
        return None
    return {"access_token": tok, "expiry": time.time() + int(info.json().get("expires_in", 600)) - 60}


class Drive:
    """Auth order: $CAMCTL_DRIVE_AUTH=gcloud|oauth forces one; otherwise gcloud (if it has Drive scope),
    then the OAuth token file."""

    def __init__(self) -> None:
        self.s = requests.Session()
        self._tok: dict = {}
        mode = os.environ.get("CAMCTL_DRIVE_AUTH", "auto")
        if mode in ("auto", "gcloud"):
            tok = _gcloud_token()
            if tok:
                self.mode = "gcloud"
                self._tok = tok
                return
            if mode == "gcloud":
                raise DriveError("gcloud token lacks Drive scope. Run: gcloud auth login --enable-gdrive-access")
        self.mode = "oauth"
        self._path = token_path()
        if not self._path.exists():
            raise DriveError(f"No Drive credentials. Run `gcloud auth login --enable-gdrive-access` "
                             f"or `camctl drive login` (token path {self._path}).")
        self._tok = json.loads(self._path.read_text())

    # -- auth ---------------------------------------------------------------
    def _access(self) -> str:
        if self._tok.get("access_token") and float(self._tok.get("expiry", 0)) > time.time() + 30:
            return self._tok["access_token"]
        if self.mode == "gcloud":
            tok = _gcloud_token()
            if not tok:
                raise DriveError("gcloud token refresh failed or lost Drive scope")
            self._tok = tok
            return tok["access_token"]
        if not self._tok.get("refresh_token"):
            raise DriveError("Drive token has no refresh_token. Run `camctl drive login`.")
        client = _client()
        r = requests.post(TOKEN_URL, data={
            "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "refresh_token": self._tok["refresh_token"], "grant_type": "refresh_token",
        }, timeout=30)
        if r.status_code != 200:
            raise DriveError(f"token refresh failed ({r.status_code}): {r.text[:300]}. Run `camctl drive login`.")
        fresh = r.json()
        self._tok["access_token"] = fresh["access_token"]
        self._tok["expiry"] = time.time() + fresh.get("expires_in", 3600) - 60
        self._path.write_text(json.dumps(self._tok, indent=2))
        return self._tok["access_token"]

    def _h(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self._access()}", **(extra or {})}

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        headers = kw.pop("headers", None)
        last = ""
        for attempt in range(6):
            try:
                r = self.s.request(method, url, headers=self._h(headers), timeout=60, **kw)
            except requests.RequestException as e:
                last = repr(e)
            else:
                if r.status_code == 401 and attempt == 0:   # token expired early: refresh once and retry
                    self._tok["expiry"] = 0
                    continue
                rate_limited = r.status_code == 403 and any(k in r.text for k in RATE_LIMIT_REASONS)
                if r.status_code not in (429, 500, 502, 503, 504) and not rate_limited:
                    if r.status_code >= 400:
                        raise DriveError(f"{method} {url} → {r.status_code}: {r.text[:400]}")
                    return r
                last = str(r.status_code)
            time.sleep(min(2 ** attempt, 30))
        raise DriveError(f"{method} {url} kept failing: {last}")

    # -- folders ------------------------------------------------------------
    def about(self) -> dict:
        return self._req("GET", f"{API}/about", params={"fields": "user(emailAddress),storageQuota"}).json()

    def children(self, folder_id: str, folders_only: bool = False, fields: str = LIST_FIELDS) -> Iterator[dict]:
        q = f"'{folder_id}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        token = None
        while True:
            params = {"q": q, "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                      "fields": f"nextPageToken, files({fields})"}
            if token:
                params["pageToken"] = token
            data = self._req("GET", f"{API}/files", params=params).json()
            yield from data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return

    def find_folder(self, name: str, parent: str = "root") -> str | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = (f"name = '{safe}' and '{parent}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false")
        found = self._req("GET", f"{API}/files", params={"q": q, "fields": "files(id)", "supportsAllDrives": "true",
                                                           "includeItemsFromAllDrives": "true"}).json()["files"]
        return found[0]["id"] if found else None

    def ensure_folder(self, name: str, parent: str = "root") -> str:
        found = self.find_folder(name, parent)
        if found:
            return found
        return self._req("POST", f"{API}/files", params={"supportsAllDrives": "true"},
                         json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).json()["id"]

    def ensure_path(self, parts: list[str], parent: str = "root") -> str:
        for part in parts:
            parent = self.ensure_folder(part, parent)
        return parent

    def walk_md5(self, folder_id: str) -> dict[str, dict]:
        """md5 → file for every file under folder_id (recursive)."""
        out: dict[str, dict] = {}
        stack = [folder_id]
        while stack:
            fid = stack.pop()
            for f in self.children(fid):
                if f["mimeType"] == FOLDER_MIME:
                    stack.append(f["id"])
                elif f.get("md5Checksum"):
                    out[f["md5Checksum"]] = f
        return out

    def walk_tree(self, folder_id: str) -> tuple[list[dict], dict[str, dict]]:
        """(files, folders) under folder_id, recursive, not trashed. Every file carries WALK_FIELDS plus "_parent"
        (the folder it was listed in) and "_path" (folder names from folder_id down to that parent); folders map
        id → {id, name, parent, path}."""
        files: list[dict] = []
        folders: dict[str, dict] = {}
        stack: list[tuple[str, tuple[str, ...]]] = [(folder_id, ())]
        while stack:
            fid, path = stack.pop()
            for f in self.children(fid, fields=WALK_FIELDS):
                if f["mimeType"] == FOLDER_MIME:
                    sub = (*path, f["name"])
                    folders[f["id"]] = {"id": f["id"], "name": f["name"], "parent": fid, "path": sub}
                    stack.append((f["id"], sub))
                else:
                    files.append({**f, "_parent": fid, "_path": path})
        return files, folders

    def update(self, file_id: str, *, add_parent: str | None = None, remove_parent: str | None = None,
               name: str | None = None, trashed: bool | None = None) -> dict:
        """Metadata-only change (move / rename / trash): content and md5 are untouched, nothing is re-uploaded."""
        params = {"supportsAllDrives": "true", "fields": "id,name,parents,md5Checksum,trashed"}
        if add_parent:
            params["addParents"] = add_parent
        if remove_parent:
            params["removeParents"] = remove_parent
        body: dict = {}
        if name is not None:
            body["name"] = name
        if trashed is not None:
            body["trashed"] = trashed
        return self._req("PATCH", f"{API}/files/{file_id}", params=params, json=body).json()

    def trash(self, file_id: str) -> dict:
        """Move to Drive trash (recoverable for 30 days). camctl never deletes permanently."""
        return self.update(file_id, trashed=True)

    def read_head(self, file_id: str, nbytes: int = 65536) -> bytes:
        """First nbytes of a file's content (e.g. a Sony clip's XML sidecar)."""
        return self._req("GET", f"{API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                         headers={"Range": f"bytes=0-{nbytes - 1}"}).content

    # -- upload -------------------------------------------------------------
    def upload(self, path: Path, folder_id: str, mime: str = "application/octet-stream",
               progress: Callable[[int, int], None] | None = None, name: str | None = None) -> dict:
        """Resumable upload; survives transient network/server errors mid-file. Drive's modifiedTime is set to the
        local mtime, so a later `drive organize` can date files that carry no EXIF or date in their name."""
        st = path.stat()
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        r = self._req("POST", UPLOAD,
                      params={"uploadType": "resumable", "supportsAllDrives": "true",
                              "fields": "id,name,size,md5Checksum"},
                      headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                      json={"name": name or path.name, "parents": [folder_id], "modifiedTime": mtime})
        session = r.headers["Location"]
        pos: int | None = 0          # None → offset unknown after an error: ask the session before sending
        known = 0                    # highest offset the server has confirmed
        failures = 0
        with path.open("rb") as fh:
            while True:
                if pos is None or pos >= size:
                    chunk, crange = b"", f"bytes */{size}"   # status query (also finalizes an empty file)
                else:
                    fh.seek(pos)
                    want = min(CHUNK, size - pos)             # never past the size declared at session start
                    chunk = fh.read(want)
                    if len(chunk) != want:
                        raise DriveError(f"{path.name} shrank while uploading")
                    crange = f"bytes {pos}-{pos + want - 1}/{size}"
                try:
                    resp = self.s.put(session, data=chunk, allow_redirects=False, timeout=300,
                                      headers=self._h({"Content-Range": crange}))
                except requests.RequestException:
                    resp = None
                code = resp.status_code if resp is not None else None
                if code in (200, 201):
                    if progress:
                        progress(size, size)
                    return resp.json()
                if code == 308:
                    rng = resp.headers.get("Range")
                    pos = int(rng.split("-")[1]) + 1 if rng else 0
                    advanced = pos > known
                    if advanced:
                        failures, known = 0, pos
                        if progress:
                            progress(pos, size)
                    if advanced or (not chunk and pos < size):
                        continue
                    # a chunk the server did not keep, or every byte sent but not finalized: back off and re-ask
                elif code in (404, 410):
                    raise DriveError(f"upload session expired for {path.name}; rerun offload to restart it")
                elif code == 401:
                    self._tok["expiry"] = 0                   # token expired mid-upload: refresh on next request
                elif code is not None and code < 500 and code != 429:
                    raise DriveError(f"upload {path.name} → {code}: {resp.text[:300]}")
                failures += 1
                if failures > 8:
                    raise DriveError(f"upload {path.name} failed repeatedly")
                time.sleep(min(2 ** failures, 60))
                pos = None


def organize(root_name: str = "Card Archive", merge: tuple[str, ...] = ("A7V Offload",), dry_run: bool = True,
             **kw):
    """Sort the card archive by year, then type. See camctl.organize.organize."""
    from .organize import organize as run
    return run(root_name=root_name, merge=merge, dry_run=dry_run, **kw)
