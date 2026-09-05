"""The upload gate passes only on a byte-proven mirror. No network: HfApi and
hf_xet are replaced by in-memory doubles that behave like the measured
hub 1.7.2 API (sync uploads the directory's CONTENTS under the prefix;
list_bucket_tree returns files with .path/.size/.xet_hash)."""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import pytest

from persona_redteaming.cloud import hf_upload as hu


class _Item:
    def __init__(self, path, size, xet_hash):
        self.type, self.path, self.size, self.xet_hash = "file", path, size, xet_hash


def _xet(data: bytes) -> str:
    return "xet-" + hashlib.sha256(data).hexdigest()[:16]


class FakeApi:
    """One bucket, a dict of remote path -> bytes."""

    store: dict[str, bytes] = {}
    synced: list[tuple[str, str, bool]] = []

    def __init__(self):
        pass

    def sync_bucket(self, local, dest_url, delete=False, quiet=True):
        FakeApi.synced.append((local, dest_url, delete))
        bucket_id, prefix = hu.parse_bucket_url(dest_url)
        for rel, size in hu.enumerate_local(Path(local)).items():
            FakeApi.store[f"{prefix}/{rel}" if prefix else rel] = (Path(local) / rel).read_bytes()

    def list_bucket_tree(self, bucket_id, prefix, recursive=True):
        for path, data in FakeApi.store.items():
            if prefix is None or path == prefix or path.startswith(prefix + "/"):
                yield _Item(path, len(data), _xet(data))


@pytest.fixture
def fake_hub(monkeypatch):
    FakeApi.store = {}
    FakeApi.synced = []
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(HfApi=FakeApi))

    class Info:
        def __init__(self, p):
            data = Path(p).read_bytes()
            self.hash, self.file_size = _xet(data), len(data)

    monkeypatch.setitem(sys.modules, "hf_xet",
                        types.SimpleNamespace(hash_files=lambda ps: [Info(p) for p in ps]))
    return FakeApi


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def test_parse_bucket_url():
    assert hu.parse_bucket_url("hf://buckets/ns/name") == ("ns/name", "")
    assert hu.parse_bucket_url("hf://buckets/ns/name/a/b/") == ("ns/name", "a/b")
    with pytest.raises(ValueError):
        hu.parse_bucket_url("s3://ns/name")
    with pytest.raises(ValueError):
        hu.parse_bucket_url("hf://buckets/onlyns")


def test_enumerate_local_walks_regular_files_only(tmp_path):
    _tree(tmp_path, {"a.txt": b"aaa", "d/b.bin": b"bb", "d/e/empty": b""})
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    assert hu.enumerate_local(tmp_path) == {"a.txt": 3, "d/b.bin": 2, "d/e/empty": 0}


def test_upload_verified_passes_on_an_exact_mirror(tmp_path, fake_hub):
    local = _tree(tmp_path / "up", {"x/1.json": b"{}", "y.safetensors": b"\x00" * 10})
    res = hu.upload_verified(local, "hf://buckets/ns/name/pre/fix")
    assert res == {"files": 2, "bytes": 12, "how": "all sizes + all xet content hashes"}
    assert fake_hub.synced == [(str(local), "hf://buckets/ns/name/pre/fix", False)]
    assert set(fake_hub.store) == {"pre/fix/x/1.json", "pre/fix/y.safetensors"}


def test_upload_verified_fails_on_a_missing_remote_file(tmp_path, fake_hub):
    local = _tree(tmp_path / "up", {"a": b"1", "b": b"2"})
    hu.upload_verified(local, "hf://buckets/ns/name/p")
    del fake_hub.store["p/b"]
    with pytest.raises(hu.VerificationFailed) as exc:
        hu.upload_verified(local, "hf://buckets/ns/name/p", verify_only=True)
    assert exc.value.failures == ["MISSING ON REMOTE: b"]


def test_upload_verified_fails_on_size_and_hash_mismatch(tmp_path, fake_hub):
    local = _tree(tmp_path / "up", {"a": b"1234", "b": b"abcd"})
    hu.upload_verified(local, "hf://buckets/ns/name/p")
    fake_hub.store["p/a"] = b"12"      # truncated transfer: size differs
    fake_hub.store["p/b"] = b"abce"    # same size, different bytes
    with pytest.raises(hu.VerificationFailed) as exc:
        hu.upload_verified(local, "hf://buckets/ns/name/p", verify_only=True)
    kinds = sorted(f.split(" ")[0] for f in exc.value.failures)
    assert kinds == ["SIZE", "XET-HASH"]


def test_upload_verified_treats_remote_extras_as_failure_unless_allowed(tmp_path, fake_hub):
    local = _tree(tmp_path / "up", {"a": b"1"})
    hu.upload_verified(local, "hf://buckets/ns/name/p")
    fake_hub.store["p/stale"] = b"old"
    with pytest.raises(hu.VerificationFailed):
        hu.upload_verified(local, "hf://buckets/ns/name/p", verify_only=True)
    assert hu.upload_verified(local, "hf://buckets/ns/name/p", verify_only=True,
                              allow_extra=True)["files"] == 1


def test_upload_verified_refuses_an_empty_or_missing_directory(tmp_path, fake_hub):
    with pytest.raises(ValueError):
        hu.upload_verified(tmp_path / "nope", "hf://buckets/ns/name")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        hu.upload_verified(tmp_path / "empty", "hf://buckets/ns/name")
    assert fake_hub.synced == []


def test_cli_exit_codes(tmp_path, fake_hub):
    local = _tree(tmp_path / "up", {"a": b"1"})
    assert hu.main([str(local), "hf://buckets/ns/name/p"]) == 0
    fake_hub.store.clear()
    assert hu.main([str(local), "hf://buckets/ns/name/p", "--verify-only"]) == 2
    assert hu.main([str(tmp_path / "missing"), "hf://buckets/ns/name/p"]) == 1
