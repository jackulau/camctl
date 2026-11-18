"""Upload a staging copy to Drive while it is still being filled; exit after the copy is marked done
(sentinel file) and a final pass verifies everything."""
import json
import sys
import time
from pathlib import Path

from camctl.offload import offload

src, root, sentinel = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])


def quiet(m: str) -> None:
    if not m.lstrip().startswith("hashed"):
        print(m, flush=True)


while True:
    done = sentinel.exists()              # read BEFORE the pass so the last pass sees every file
    r = offload([src], root_name=root, scope="all", jobs=8, log=quiet)   # every file, not just non-camera
    s = r.summary()
    print("PASS " + json.dumps({k: s[k] for k in ("files", "uploaded", "already_on_drive", "failed")}), flush=True)
    if done and not r.failed:
        print("FINAL " + json.dumps(s), flush=True)
        break
    if s["uploaded"] == 0:
        time.sleep(20)
