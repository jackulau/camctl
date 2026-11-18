"""Persistent md5 cache: a re-offload only reads files that are new or changed."""

from __future__ import annotations

import hashlib
import os

from camctl import offload as off


def _count_reads(monkeypatch):
    reads = []
    real = off._md5

    def spy(path, buf):
        reads.append(path.name)
        return real(path, buf)

    monkeypatch.setattr(off, "_md5", spy)
    return reads


def _file(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_unchanged_file_is_not_read_again(tmp_path, monkeypatch):
    reads = _count_reads(monkeypatch)
    p = _file(tmp_path, "DSC00001.JPG", b"photo")
    c = off.HashCache()
    assert c.md5("vol", "DCIM/DSC00001.JPG", p, off._sig(p)) == hashlib.md5(b"photo").hexdigest()
    c.save()

    c2 = off.HashCache()                         # a later run: loaded from disk
    assert c2.md5("vol", "DCIM/DSC00001.JPG", p, off._sig(p)) == hashlib.md5(b"photo").hexdigest()
    assert reads == ["DSC00001.JPG"] and c2.hits == 1


def test_changed_size_or_mtime_is_rehashed(tmp_path, monkeypatch):
    reads = _count_reads(monkeypatch)
    p = _file(tmp_path, "a.JPG", b"one")
    c = off.HashCache()
    c.md5("vol", "a.JPG", p, off._sig(p))
    p.write_bytes(b"two")                        # same size, new content, new mtime
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
    assert c.md5("vol", "a.JPG", p, off._sig(p)) == hashlib.md5(b"two").hexdigest()
    assert reads == ["a.JPG", "a.JPG"]


def test_other_card_with_same_path_does_not_share_hashes(tmp_path, monkeypatch):
    reads = _count_reads(monkeypatch)
    p = _file(tmp_path, "a.JPG", b"x")
    c = off.HashCache()
    c.md5("uuid:card-1", "DCIM/a.JPG", p, off._sig(p))
    c.md5("uuid:card-2", "DCIM/a.JPG", p, off._sig(p))
    assert len(reads) == 2


def test_rehash_reads_everything_and_refreshes(tmp_path, monkeypatch):
    reads = _count_reads(monkeypatch)
    p = _file(tmp_path, "a.JPG", b"x")
    off.HashCache().md5("vol", "a.JPG", p, off._sig(p))
    c = off.HashCache(rehash=True)
    c.md5("vol", "a.JPG", p, off._sig(p))
    assert len(reads) == 2 and c.hits == 0


def test_prune_drops_deleted_files_and_stale_cards(tmp_path):
    p = _file(tmp_path, "a.JPG", b"x")
    c = off.HashCache()
    c.md5("vol", "a.JPG", p, off._sig(p))
    c.md5("vol", "gone.JPG", p, off._sig(p))
    c.data["volumes"]["old-card"] = {"files": {"x": [1, 1, "m"]}, "last_seen": 0}
    c.prune("vol", {"a.JPG"})
    assert set(c.data["volumes"]["vol"]["files"]) == {"a.JPG"}
    assert "old-card" not in c.data["volumes"]


def test_corrupt_cache_file_starts_empty(tmp_path):
    off.HASH_CACHE.write_text("{not json")
    assert off.HashCache().data == {"version": 1, "volumes": {}}


def test_missing_file_raises_oserror(tmp_path):
    import pytest
    with pytest.raises(OSError):
        off.HashCache().md5("vol", "nope", tmp_path / "nope", None)
