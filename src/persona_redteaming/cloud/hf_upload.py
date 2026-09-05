#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["huggingface_hub>=1.7.0"]
# ///
# The header above lets the file run standalone on a box (`uv run --no-project`);
# inside the package it is `persona_redteaming.cloud.hf_upload`.
"""Byte-verified upload of a local directory tree to a Hugging Face BUCKET.

This is the project's upload gate. `upload_verified(local_dir, dest_url)`
returns only when every local file is proven present on the bucket with
matching byte size and (when hf_xet is importable, which it is wherever
huggingface_hub>=1.7 is installed on x86_64/arm64) a matching Xet content
hash computed LOCALLY and compared with the server-reported xet_hash. If
hf_xet is unavailable it falls back to spot re-downloading the N largest plus
N random files and comparing sha256. A plain uploader returning 0 has
published zero files before; the gate's own `VERIFIED: N files, M bytes` line
is the evidence, and it raises `VerificationFailed` otherwise.

CLI:
  python -m persona_redteaming.cloud.hf_upload LOCAL_DIR \
      hf://buckets/NAMESPACE/BUCKET[/PREFIX] [opts]

Options:
  --verify-only     Skip the upload; only verify LOCAL_DIR against the bucket.
  --delete          Pass --delete to sync (remove remote files absent locally).
  --spot N          Fallback spot-check count per category (default 3).
  --allow-extra     Do not fail on remote files that have no local counterpart
                    (default: extra files are reported and FAIL the gate,
                    because a verified prefix should mirror the local dir;
                    use --allow-extra for append-style prefixes).

Exit codes: 0 verified OK; 1 usage/runtime error; 2 verification FAILED.

Token: read by huggingface_hub from HF_TOKEN / stored login. This module
never receives, stores, or prints the token.

Measured facts this design relies on (2026-09-04, hub 1.7.2, hf CLI 1.7.2):
  - `HfApi.list_bucket_tree(bucket, prefix, recursive=True)` returns
    BucketFile objects with .path, .size, .xet_hash.
  - `hf_xet.hash_files([paths])` reproduces the server xet_hash exactly,
    including the all-zeros hash for empty files.
  - `HfApi.sync_bucket(local, "hf://buckets/ns/name/prefix")` uploads the
    CONTENTS of local into the prefix (rsync semantics, size+mtime skip).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import tempfile
from pathlib import Path

BUCKET_URL_PREFIX = "hf://buckets/"
DEFAULT_BUCKET = "hf://buckets/unrulyabstractions/persona-redteaming"


class VerificationFailed(RuntimeError):
    """The bucket does not hold what the local directory holds."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__(f"{len(failures)} problems: " + "; ".join(failures[:5]))
        self.failures = failures


def parse_bucket_url(url: str) -> tuple[str, str]:
    """Split hf://buckets/ns/name[/prefix] -> (bucket_id, prefix). Raises
    ValueError on anything else."""
    if not url.startswith(BUCKET_URL_PREFIX):
        raise ValueError(f"destination must start with {BUCKET_URL_PREFIX} (got {url!r})")
    rest = url[len(BUCKET_URL_PREFIX):].strip("/")
    parts = rest.split("/")
    if len(parts) < 2:
        raise ValueError(f"destination must be {BUCKET_URL_PREFIX}NAMESPACE/BUCKET[/PREFIX]")
    bucket_id = "/".join(parts[:2])
    prefix = "/".join(parts[2:])
    return bucket_id, prefix


def enumerate_local(local_dir: Path) -> dict[str, int]:
    """relpath (posix) -> size in bytes, for every regular file under local_dir."""
    out: dict[str, int] = {}
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            p = Path(root) / name
            if not p.is_file():  # skip broken symlinks, sockets, etc.
                continue
            out[p.relative_to(local_dir).as_posix()] = p.stat().st_size
    return out


def enumerate_remote(api, bucket_id: str, prefix: str) -> dict[str, tuple[int, str]]:
    """relpath (relative to prefix) -> (size, xet_hash)."""
    out: dict[str, tuple[int, str]] = {}
    for item in api.list_bucket_tree(bucket_id, prefix or None, recursive=True):
        if getattr(item, "type", None) != "file":
            continue
        path = item.path
        if prefix:
            if path == prefix or not path.startswith(prefix + "/"):
                continue
            rel = path[len(prefix) + 1:]
        else:
            rel = path
        out[rel] = (item.size, item.xet_hash)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_hashes_xet(local_dir: Path, rels: list[str],
                      remote: dict[str, tuple[int, str]]) -> list[str]:
    """Compare local xet hash of every file with the server xet_hash."""
    import hf_xet  # bundled with huggingface_hub on x86_64/arm64

    failures = []
    paths = [str(local_dir / r) for r in rels]
    infos = hf_xet.hash_files(paths)
    for rel, info in zip(rels, infos):
        rsize, rhash = remote[rel]
        if info.hash != rhash:
            failures.append(f"XET-HASH MISMATCH {rel}: local {info.hash} remote {rhash}")
        if info.file_size != rsize:
            failures.append(f"SIZE MISMATCH (hash pass) {rel}: local {info.file_size} remote {rsize}")
    return failures


def verify_hashes_spot(api, bucket_id: str, prefix: str, local_dir: Path,
                       rels: list[str], spot_n: int) -> list[str]:
    """Fallback: re-download N largest + N random files, compare sha256."""
    by_size = sorted(rels, key=lambda r: (local_dir / r).stat().st_size, reverse=True)
    chosen = list(dict.fromkeys(
        by_size[:spot_n] + random.sample(rels, min(spot_n, len(rels)))))
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        pairs = []
        for i, rel in enumerate(chosen):
            remote_path = f"{prefix}/{rel}" if prefix else rel
            pairs.append((remote_path, os.path.join(tmp, str(i))))
        api.download_bucket_files(bucket_id, files=pairs, raise_on_missing_files=True)
        for rel, (_rp, lp) in zip(chosen, pairs):
            got, want = sha256_file(Path(lp)), sha256_file(local_dir / rel)
            if got != want:
                failures.append(f"SHA256 MISMATCH {rel}: downloaded {got} local {want}")
    print(f"spot-checked {len(chosen)} files by re-download+sha256: {chosen}")
    return failures


def upload_verified(local_dir: Path, dest_url: str, *, verify_only: bool = False,
                    delete: bool = False, spot: int = 3, allow_extra: bool = False,
                    log=print) -> dict:
    """Upload `local_dir` under `dest_url` and prove every byte landed.

    Inputs: a local directory and an `hf://buckets/ns/name[/prefix]` URL.
    Output: `{"files": N, "bytes": M, "how": ...}` once every local file is
    present remotely with the same size and content hash. Refuses an empty
    or non-existent directory (ValueError) and raises `VerificationFailed`
    listing every missing, extra, or mismatched file; nothing is recorded as
    uploaded on a failure. With `verify_only` nothing is sent.
    """
    from huggingface_hub import HfApi

    local_dir = Path(local_dir).resolve()
    if not local_dir.is_dir():
        raise ValueError(f"{local_dir} is not a directory")
    bucket_id, prefix = parse_bucket_url(dest_url)

    local = enumerate_local(local_dir)
    if not local:
        raise ValueError(f"no files found under {local_dir}")
    total_bytes = sum(local.values())
    log(f"local:  {len(local)} files, {total_bytes} bytes under {local_dir}")
    log(f"target: bucket {bucket_id!r} prefix {prefix!r}")

    api = HfApi()

    if not verify_only:
        api.sync_bucket(str(local_dir), dest_url, delete=delete, quiet=True)
        log("sync: completed")

    remote = enumerate_remote(api, bucket_id, prefix)
    log(f"remote: {len(remote)} files, {sum(s for s, _ in remote.values())} bytes")

    failures: list[str] = []
    missing = sorted(set(local) - set(remote))
    extra = sorted(set(remote) - set(local))
    for rel in missing:
        failures.append(f"MISSING ON REMOTE: {rel}")
    for rel in extra:
        msg = f"EXTRA ON REMOTE (no local counterpart): {rel}"
        if allow_extra:
            log(f"warning: {msg}")
        else:
            failures.append(msg)
    common = sorted(set(local) & set(remote))
    for rel in common:
        if local[rel] != remote[rel][0]:
            failures.append(f"SIZE MISMATCH {rel}: local {local[rel]} remote {remote[rel][0]}")

    # Content-hash verification for every common file whose size matched.
    sized_ok = [r for r in common if local[r] == remote[r][0]]
    try:
        import hf_xet  # noqa: F401
        have_xet = True
    except ImportError:
        have_xet = False
    if sized_ok:
        if have_xet:
            failures += verify_hashes_xet(local_dir, sized_ok, remote)
            log(f"xet-hash verified {len(sized_ok)} files locally against server hashes")
        else:
            log("hf_xet not importable; falling back to spot re-download")
            failures += verify_hashes_spot(api, bucket_id, prefix, local_dir,
                                           sized_ok, spot)

    if failures:
        log(f"\nVERIFICATION FAILED ({len(failures)} problems):")
        for f in failures:
            log(f"  {f}")
        raise VerificationFailed(failures)
    how = ("all sizes + all xet content hashes" if have_xet
           else "all sizes; content spot-checked by re-download+sha256")
    log(f"\nVERIFIED: {len(local)} files, {total_bytes} bytes ({how})")
    return {"files": len(local), "bytes": total_bytes, "how": how}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("local_dir")
    ap.add_argument("dest_url")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--spot", type=int, default=3)
    ap.add_argument("--allow-extra", action="store_true")
    args = ap.parse_args(argv)
    try:
        upload_verified(Path(args.local_dir), args.dest_url, verify_only=args.verify_only,
                        delete=args.delete, spot=args.spot, allow_extra=args.allow_extra)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except VerificationFailed:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
