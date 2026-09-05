"""genlog/v1 reader: validation, amendment resolution, refuse rules.

Contract: temp/50-genlog-schema.md (orchestrator-owned), including the
schema v1.1 clarifications. The provider WRITES `generations.jsonl`; this
module READS it. Ids are truth; text is audit metadata.

Refusal granularity (schema v1.1, from the temp/53 review D3):
- FILE-FATAL (`GenlogValidationError`, the whole run is refused): bad JSON,
  unknown/missing schema tag, missing or invalid `invoke_idx`, duplicate
  `invoke_idx`, malformed amendment lines, and an amendment that precedes
  its record (v1.1: amendments MUST follow the record they amend; a forward
  reference is file corruption).
- RECORD-LOCAL (the record is marked malformed and REFUSED in the manifest;
  the rest of the file harvests normally): every other shape violation —
  missing fields, bad enums, non-int token id lists, `completion_logprobs`
  not a flat float list (v1.1 shape) or length-mismatched against
  `completion_token_ids`, ids/source null-consistency.
  Measured blast radius that forced this split: one hf-raw-shaped record
  used to kill a 6-good-record file (temp/53 F1).

Amendment resolution (append-only log, never rewrite):
- a full record carries its own `status` (normally "retained");
- amendment lines `{"schema": "genlog/v1", "amends": <invoke_idx>, ...}`
  override it, last-writer-wins in file order;
- `reverted`: harvestable as "rejected attempt" data, flagged;
- `healed`: replay the RAW emission from this log (which is all we ever
  replay), never the healed message; flagged.

Refuse rules (per record, from the contract's "Harvester obligations"):
- record-local malformation (above);
- tripwire mismatch (prompt_tokens_local vs prompt_tokens_server vs
  len(prompt_token_ids)) on backends that assert it (vllm, hf-raw); note
  v1.1 also has the provider null out ids on tripwire fire, which lands in
  the null-ids refusal below;
- `completion_ids_source` null / `completion_token_ids` null, unless
  text-mode is explicitly requested (then the raw completion text is
  tokenized with the harvest tokenizer and the record is flagged
  fidelity="text");
- `template_sha256` differing from the harvest-time tokenizer's template;
- backend `hf-chat`: NEVER harvestable, text-mode included (v1.1; no
  prompt ids exist to replay).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

GENLOG_SCHEMA = "genlog/v1"
GENLOG_FILENAME = "generations.jsonl"

BACKENDS = {"vllm", "hf-chat", "hf-raw"}
STATUSES = {"retained", "reverted", "healed"}
COMPLETION_ID_SOURCES = {"server", "inverted-verified", None}

# Every key must be PRESENT in a full record (null allowed where the
# contract allows it). Presence is part of the contract.
FULL_RECORD_FIELDS = (
    "schema",
    "invoke_idx",
    "ts",
    "backend",
    "endpoint",
    "hf_provider",
    "model",
    "model_revision",
    "tokenizer_revision",
    "template_sha256",
    "sampling",
    "prompt_token_ids",
    "prompt_text_sha256",
    "prompt_tokens_local",
    "prompt_tokens_server",
    "completion_text_raw",
    "completion_token_ids",
    "completion_ids_source",
    "completion_logprobs",
    "usage",
    "finish_reason",
    "status",
)


class GenlogValidationError(Exception):
    """File-level corruption: refuse the whole run (v1.1 granularity)."""


@dataclass
class GenRecord:
    """One resolved invoke() record."""

    invoke_idx: int
    raw: dict
    status: str  # status field on the full record as written
    resolved_status: str  # after amendment resolution (last-writer-wins)
    malformed: str | None = None  # record-local violation detail (v1.1)
    amendments: list[dict] = field(default_factory=list)

    @property
    def backend(self) -> str:
        return self.raw["backend"]

    @property
    def prompt_token_ids(self) -> list[int] | None:
        return self.raw["prompt_token_ids"]

    @property
    def completion_token_ids(self) -> list[int] | None:
        return self.raw["completion_token_ids"]

    @property
    def completion_logprobs(self) -> list[float] | None:
        return self.raw["completion_logprobs"]

    @property
    def completion_text_raw(self) -> str:
        return self.raw["completion_text_raw"]

    @property
    def template_sha256(self) -> str:
        return self.raw["template_sha256"]


@dataclass
class Refusal:
    invoke_idx: int
    reason: str
    detail: str


def _is_int_list(x) -> bool:
    return isinstance(x, list) and all(isinstance(i, int) for i in x)


def _record_local_violation(obj: dict) -> str | None:
    """Shape checks beyond the identity fields. Returns a detail string for
    the FIRST violation found, or None. Violations refuse the RECORD, not
    the run (schema v1.1)."""
    missing = [k for k in FULL_RECORD_FIELDS if k not in obj]
    if missing:
        return f"record missing fields {missing}"
    if obj["backend"] not in BACKENDS:
        return f"bad backend {obj['backend']!r}"
    if obj["status"] not in STATUSES:
        return f"bad status {obj['status']!r}"
    if obj["completion_ids_source"] not in COMPLETION_ID_SOURCES:
        return f"bad completion_ids_source {obj['completion_ids_source']!r}"
    if obj["prompt_token_ids"] is not None and not _is_int_list(obj["prompt_token_ids"]):
        return "prompt_token_ids not a list of ints"
    if obj["completion_token_ids"] is not None and not _is_int_list(
        obj["completion_token_ids"]
    ):
        return "completion_token_ids not a list of ints"
    if (obj["completion_token_ids"] is None) != (obj["completion_ids_source"] is None):
        return (
            "completion_token_ids and completion_ids_source must be null "
            "together (contract: source describes where the ids came from)"
        )
    if not isinstance(obj["completion_text_raw"], str):
        return "completion_text_raw not a string"
    if not isinstance(obj["sampling"], dict):
        return "sampling not an object"
    lp = obj["completion_logprobs"]
    if lp is not None:
        if not isinstance(lp, list) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in lp
        ):
            return (
                "completion_logprobs not a flat list of numbers (schema v1.1: "
                "flat per-completion-token floats aligned with "
                "completion_token_ids; richer objects belong in "
                "completion_logprobs_detail)"
            )
        if obj["completion_token_ids"] is not None and len(lp) != len(
            obj["completion_token_ids"]
        ):
            return (
                f"completion_logprobs length {len(lp)} != "
                f"completion_token_ids length {len(obj['completion_token_ids'])}"
            )
    return None


def _validate_amendment(obj: dict, lineno: int) -> None:
    if obj.get("schema") != GENLOG_SCHEMA:
        raise GenlogValidationError(
            f"line {lineno}: amendment schema {obj.get('schema')!r} != {GENLOG_SCHEMA!r}"
        )
    if not isinstance(obj["amends"], int):
        raise GenlogValidationError(f"line {lineno}: amends must be an int invoke_idx")
    if obj.get("status") not in ("reverted", "healed"):
        raise GenlogValidationError(
            f"line {lineno}: amendment status {obj.get('status')!r} "
            "(must be reverted or healed)"
        )
    if obj["status"] == "healed" and "healed_diff_sha256" not in obj:
        raise GenlogValidationError(
            f"line {lineno}: healed amendment missing healed_diff_sha256"
        )


def load_genlog(path: str | Path) -> list[GenRecord]:
    """Parse generations.jsonl; resolve amendments in file order.

    Raises GenlogValidationError only on FILE-LEVEL corruption; record-local
    shape violations come back as records with `malformed` set (refused
    per-record by check_record). Returns records ordered by invoke_idx.
    """
    path = Path(path)
    if path.is_dir():
        path = path / GENLOG_FILENAME
    if not path.is_file():
        raise GenlogValidationError(f"no genlog at {path}")

    records: dict[int, GenRecord] = {}
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise GenlogValidationError(f"line {lineno}: invalid JSON: {e}") from e
            if not isinstance(obj, dict):
                raise GenlogValidationError(f"line {lineno}: not a JSON object")

            if "amends" in obj:
                _validate_amendment(obj, lineno)
                idx = obj["amends"]
                if idx not in records:
                    raise GenlogValidationError(
                        f"line {lineno}: amendment references invoke_idx {idx} "
                        "which has not appeared yet (v1.1: amendments must "
                        "follow their record; forward reference = corruption)"
                    )
                rec = records[idx]
                rec.amendments.append(obj)
                rec.resolved_status = obj["status"]  # last writer wins
                continue

            # full record: identity fields are file-fatal (v1.1)
            if obj.get("schema") != GENLOG_SCHEMA:
                raise GenlogValidationError(
                    f"line {lineno}: schema {obj.get('schema')!r} != {GENLOG_SCHEMA!r}"
                )
            idx = obj.get("invoke_idx")
            if not isinstance(idx, int) or idx < 0:
                raise GenlogValidationError(f"line {lineno}: bad invoke_idx {idx!r}")
            if idx in records:
                raise GenlogValidationError(f"line {lineno}: duplicate invoke_idx {idx}")

            violation = _record_local_violation(obj)
            status = obj.get("status") if obj.get("status") in STATUSES else "retained"
            records[idx] = GenRecord(
                invoke_idx=idx,
                raw=obj,
                status=status,
                resolved_status=status,
                malformed=violation,
            )

    return [records[i] for i in sorted(records)]


def template_sha256_of(tokenizer) -> str:
    """sha256 of the tokenizer's chat template string (utf-8).

    Matches the pinning convention in temp/10-template-forensics.md (Qwen3:
    a55ee1b1660128b7..., the template embedded in tokenizer_config.json).
    """
    template = tokenizer.chat_template
    if template is None:
        raise GenlogValidationError("harvest tokenizer has no chat_template")
    if isinstance(template, dict):  # multi-template checkpoints: use "default"
        template = template.get("default")
        if template is None:
            raise GenlogValidationError("harvest tokenizer chat_template has no default")
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def check_record(
    rec: GenRecord,
    *,
    harvest_template_sha256: str,
    text_mode: bool = False,
) -> Refusal | None:
    """Apply the contract's refuse rules. Returns a Refusal or None (ok)."""
    idx = rec.invoke_idx

    if rec.malformed is not None:
        return Refusal(idx, "malformed-record", rec.malformed)

    r = rec.raw

    if r["backend"] == "hf-chat":
        return Refusal(
            idx,
            "backend-not-replayable",
            "hf-chat records are never harvestable, text-mode included "
            "(schema v1.1: no prompt token ids exist to replay)",
        )

    if r["template_sha256"] != harvest_template_sha256:
        return Refusal(
            idx,
            "template-sha-mismatch",
            f"record template_sha256 {str(r['template_sha256'])[:16]}... != harvest "
            f"tokenizer template {harvest_template_sha256[:16]}...",
        )

    # Tripwire: vllm and hf-raw assert local == server prompt token counts.
    if r["prompt_token_ids"] is None:
        return Refusal(idx, "no-prompt-token-ids", "prompt_token_ids is null")
    n_local = len(r["prompt_token_ids"])
    if r["prompt_tokens_local"] is not None and r["prompt_tokens_local"] != n_local:
        return Refusal(
            idx,
            "tripwire-mismatch",
            f"prompt_tokens_local {r['prompt_tokens_local']} != "
            f"len(prompt_token_ids) {n_local}",
        )
    if (
        r["prompt_tokens_server"] is not None
        and r["prompt_tokens_server"] != n_local
    ):
        return Refusal(
            idx,
            "tripwire-mismatch",
            f"prompt_tokens_server {r['prompt_tokens_server']} != "
            f"len(prompt_token_ids) {n_local}",
        )

    if r["completion_ids_source"] is None and not text_mode:
        return Refusal(
            idx,
            "null-completion-ids",
            "completion_ids_source is null and text-mode was not requested "
            "(contract: refuse unless text-mode explicitly requested; v1.1 "
            "tripwire-fired records land here by design)",
        )
    if r["completion_ids_source"] is None and text_mode and not r["completion_text_raw"]:
        return Refusal(
            idx,
            "empty-completion-text",
            "text-mode requested but completion_text_raw is empty",
        )
    return None


def completion_ids_for_replay(
    rec: GenRecord, tokenizer, *, text_mode: bool
) -> tuple[list[int], str]:
    """Return (completion token ids, fidelity flag).

    fidelity: "ids" when the logged ids are used (truth), "text" when the raw
    completion text was tokenized with the harvest tokenizer (text-mode only;
    lossy per temp/20-critique-science, flagged in the manifest).
    """
    if rec.completion_token_ids is not None:
        return list(rec.completion_token_ids), "ids"
    if not text_mode:
        raise ValueError("record has null completion ids and text_mode is off")
    ids = tokenizer(rec.completion_text_raw, add_special_tokens=False)["input_ids"]
    return list(ids), "text"
