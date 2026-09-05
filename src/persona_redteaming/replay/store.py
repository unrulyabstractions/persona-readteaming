"""Activation store: safetensors (npz fallback) + manifest.json.

Layout under <out_dir>/:
    manifest.json
    layer_<L>.safetensors      one file per captured layer
        tensor key = "<run_id>/<invoke_idx:05d>/<span_kind>/<span_idx:05d>"
        (the (run_id, invoke_idx, layer, span_kind, span_idx) key: the layer
        is the file, the rest is the tensor key)

The manifest carries model/tokenizer/template revisions, gate stats, harvest
params, and per-span (message index, char span, status flag) so every vector
joins back to transcript semantics. It contains NO wall-clock timestamps:
re-harvesting the same inputs must produce byte-identical files (determinism
is an E2E-tested property; see tests/test_e2e.py).

Fallback: when safetensors is unavailable the same keys go into
layer_<L>.npz with bf16 tensors upcast to float32 (numpy has no bf16); the
manifest records storage="npz-float32-fallback".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import torch

try:
    from safetensors.torch import save_file as _st_save

    HAVE_SAFETENSORS = True
except Exception:  # pragma: no cover - exercised only on stripped installs
    _st_save = None
    HAVE_SAFETENSORS = False

MANIFEST_SCHEMA = "replay-manifest/v1"


def span_key(run_id: str, invoke_idx: int, span_kind: str, span_idx: int) -> str:
    return f"{run_id}/{invoke_idx:05d}/{span_kind}/{span_idx:05d}"


class ActStore:
    def __init__(self, out_dir: str | Path, run_id: str):
        self.out_dir = Path(out_dir)
        self.run_id = run_id
        # layer -> {key: tensor (cpu)}
        self._tensors: dict[int, dict[str, torch.Tensor]] = {}

    def add(
        self,
        invoke_idx: int,
        layer: int,
        span_kind: str,
        span_idx: int,
        tensor: torch.Tensor,
    ) -> str:
        key = span_key(self.run_id, invoke_idx, span_kind, span_idx)
        bucket = self._tensors.setdefault(layer, {})
        if key in bucket:
            raise ValueError(f"duplicate span key {key} for layer {layer}")
        bucket[key] = tensor.detach().cpu().contiguous()
        return key

    def finalize(self, manifest: dict) -> dict:
        """Write layer files + manifest.json. Returns {filename: sha256}."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        storage = "safetensors" if HAVE_SAFETENSORS else "npz-float32-fallback"
        files: dict[str, str] = {}
        for layer in sorted(self._tensors):
            tensors = dict(sorted(self._tensors[layer].items()))
            if HAVE_SAFETENSORS:
                fname = f"layer_{layer:02d}.safetensors"
                _st_save(tensors, self.out_dir / fname)
            else:
                import numpy as np

                fname = f"layer_{layer:02d}.npz"
                arrays = {
                    k: v.to(torch.float32).numpy() if v.dtype == torch.bfloat16
                    else v.numpy()
                    for k, v in tensors.items()
                }
                np.savez(self.out_dir / fname, **arrays)
            files[fname] = _sha256_file(self.out_dir / fname)

        manifest = dict(manifest)
        manifest["manifest_schema"] = MANIFEST_SCHEMA
        manifest["storage"] = storage
        manifest["files"] = files
        manifest_bytes = json.dumps(
            manifest, indent=1, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        (self.out_dir / "manifest.json").write_bytes(manifest_bytes)
        files["manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
        return files


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(acts_dir: str | Path) -> dict:
    with open(Path(acts_dir) / "manifest.json", encoding="utf-8") as f:
        return json.load(f)


def load_layer(acts_dir: str | Path, layer: int) -> dict[str, torch.Tensor]:
    """Load one layer file back as {span_key: tensor}."""
    acts_dir = Path(acts_dir)
    st_path = acts_dir / f"layer_{layer:02d}.safetensors"
    if st_path.exists():
        from safetensors.torch import load_file

        return load_file(st_path)
    npz_path = acts_dir / f"layer_{layer:02d}.npz"
    if npz_path.exists():
        import numpy as np

        with np.load(npz_path) as z:
            return {k: torch.from_numpy(z[k]) for k in z.files}
    raise FileNotFoundError(f"no layer {layer} file under {acts_dir}")
