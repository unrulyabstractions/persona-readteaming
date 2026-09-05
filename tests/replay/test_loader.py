"""Unit tests for the genlog/v1 loader: validation, amendments, refusals.

Refusal granularity follows schema v1.1 (temp/53 D3): record-local shape
violations refuse the record via check_record; only file-level corruption
(bad JSON, unknown schema, bad/duplicate invoke_idx, malformed or
forward-referencing amendments) raises GenlogValidationError.
"""

import json

import pytest

from persona_redteaming.replay.loader import (
    GenlogValidationError,
    check_record,
    completion_ids_for_replay,
    load_genlog,
)

TEMPLATE_SHA = "a" * 64


def full_record(invoke_idx=0, **overrides) -> dict:
    rec = {
        "schema": "genlog/v1",
        "invoke_idx": invoke_idx,
        "ts": "2026-09-04T12:00:00Z",
        "backend": "vllm",
        "endpoint": "local://test",
        "hf_provider": None,
        "model": "Qwen/Qwen3-1.7B",
        "model_revision": "r",
        "tokenizer_revision": "r",
        "template_sha256": TEMPLATE_SHA,
        "sampling": {"temperature": 1.0, "top_p": None, "max_tokens": 16, "seed": 0},
        "prompt_token_ids": [1, 2, 3],
        "prompt_text_sha256": "b" * 64,
        "prompt_tokens_local": 3,
        "prompt_tokens_server": 3,
        "completion_text_raw": "hi",
        "completion_token_ids": [4, 5],
        "completion_ids_source": "server",
        "completion_logprobs": [-0.1, -0.2],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        "finish_reason": "stop",
        "status": "retained",
    }
    rec.update(overrides)
    return rec


def write_genlog(tmp_path, lines):
    p = tmp_path / "generations.jsonl"
    with open(p, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return p


def load_one(tmp_path, lines):
    return load_genlog(write_genlog(tmp_path, lines))


def test_valid_roundtrip(tmp_path):
    recs = load_one(tmp_path, [full_record(0), full_record(1)])
    assert [r.invoke_idx for r in recs] == [0, 1]
    assert all(r.resolved_status == "retained" for r in recs)


def _refusal_of(tmp_path, rec_dict):
    recs = load_one(tmp_path, [rec_dict])
    return check_record(recs[0], harvest_template_sha256=TEMPLATE_SHA)


def test_missing_field_is_record_local(tmp_path):
    """v1.1 granularity (temp/53 D3): shape violations refuse the RECORD."""
    bad = full_record()
    del bad["completion_ids_source"]
    r = _refusal_of(tmp_path, bad)
    assert r is not None and r.reason == "malformed-record"
    assert "missing fields" in r.detail


def test_wrong_schema_is_file_fatal(tmp_path):
    with pytest.raises(GenlogValidationError, match="schema"):
        load_one(tmp_path, [full_record(schema="genlog/v2")])


def test_duplicate_invoke_idx_is_file_fatal(tmp_path):
    with pytest.raises(GenlogValidationError, match="duplicate"):
        load_one(tmp_path, [full_record(0), full_record(0)])


def test_bad_json_is_file_fatal(tmp_path):
    p = tmp_path / "generations.jsonl"
    p.write_text(json.dumps(full_record()) + "\nnot json\n")
    with pytest.raises(GenlogValidationError, match="invalid JSON"):
        load_genlog(p)


def test_ids_source_null_consistency_is_record_local(tmp_path):
    for rec in (
        full_record(completion_ids_source=None),
        full_record(completion_token_ids=None, completion_logprobs=None),
    ):
        r = _refusal_of(tmp_path, rec)
        assert r is not None and r.reason == "malformed-record"
        assert "null together" in r.detail


def test_logprob_length_mismatch_is_record_local(tmp_path):
    r = _refusal_of(tmp_path, full_record(completion_logprobs=[-0.1]))
    assert r is not None and r.reason == "malformed-record"
    assert "length" in r.detail


def test_dict_shaped_logprobs_refused_record_local(tmp_path):
    """The temp/53 F1 reproducer: hf-raw wrote a dict; v1.1 pins the flat
    list. The bad record is refused; the rest of the file harvests."""
    bad = full_record(
        1,
        completion_logprobs={
            "tokens": ["a", "b"],
            "token_logprobs": [-0.1, -0.2],
            "text_offset": [0, 1],
        },
    )
    recs = load_one(tmp_path, [full_record(0), bad, full_record(2)])
    assert [r.invoke_idx for r in recs] == [0, 1, 2]
    refusals = [
        check_record(r, harvest_template_sha256=TEMPLATE_SHA) for r in recs
    ]
    assert refusals[0] is None and refusals[2] is None  # blast radius: none
    assert refusals[1].reason == "malformed-record"
    assert "flat list" in refusals[1].detail


def test_list_of_dicts_logprobs_refused_record_local(tmp_path):
    """hf-chat shape: [{token, logprob}, ...] is also not the v1.1 flat list."""
    r = _refusal_of(
        tmp_path,
        full_record(completion_logprobs=[{"token": "a", "logprob": -0.1}] * 2),
    )
    assert r is not None and r.reason == "malformed-record"


def test_bad_backend_is_record_local(tmp_path):
    r = _refusal_of(tmp_path, full_record(backend="openrouter"))
    assert r is not None and r.reason == "malformed-record"


def test_amendment_resolution_last_wins(tmp_path):
    recs = load_one(
        tmp_path,
        [
            full_record(0),
            {"schema": "genlog/v1", "amends": 0, "status": "reverted", "ts": "t"},
            {
                "schema": "genlog/v1",
                "amends": 0,
                "status": "healed",
                "healed_diff_sha256": "c" * 64,
                "ts": "t",
            },
        ],
    )
    assert recs[0].status == "retained"
    assert recs[0].resolved_status == "healed"
    assert len(recs[0].amendments) == 2


def test_amendment_forward_reference_is_file_fatal(tmp_path):
    """v1.1: amendments must FOLLOW their record; forward ref = corruption."""
    with pytest.raises(GenlogValidationError, match="not appeared yet"):
        load_one(
            tmp_path,
            [
                {"schema": "genlog/v1", "amends": 0, "status": "reverted", "ts": "t"},
                full_record(0),
            ],
        )


def test_amendment_unknown_target_rejected(tmp_path):
    with pytest.raises(GenlogValidationError, match="not appeared yet"):
        load_one(
            tmp_path,
            [{"schema": "genlog/v1", "amends": 9, "status": "reverted", "ts": "t"}],
        )


def test_healed_amendment_needs_diff_sha(tmp_path):
    with pytest.raises(GenlogValidationError, match="healed_diff_sha256"):
        load_one(
            tmp_path,
            [
                full_record(0),
                {"schema": "genlog/v1", "amends": 0, "status": "healed", "ts": "t"},
            ],
        )


def _check(rec_dict, tmp_path, **kw):
    recs = load_one(tmp_path, [rec_dict])
    return check_record(recs[0], harvest_template_sha256=TEMPLATE_SHA, **kw)


def test_refuse_hf_chat_even_in_text_mode(tmp_path):
    rec = full_record(
        backend="hf-chat",
        prompt_token_ids=None,
        completion_token_ids=None,
        completion_ids_source=None,
        completion_logprobs=None,
    )
    for kw in ({}, {"text_mode": True}):  # v1.1: text-mode included
        r = _check(rec, tmp_path, **kw)
        assert r is not None and r.reason == "backend-not-replayable"


def test_refuse_template_mismatch(tmp_path):
    r = _check(full_record(template_sha256="f" * 64), tmp_path)
    assert r is not None and r.reason == "template-sha-mismatch"


def test_refuse_tripwire_local(tmp_path):
    r = _check(full_record(prompt_tokens_local=99), tmp_path)
    assert r is not None and r.reason == "tripwire-mismatch"


def test_refuse_tripwire_server(tmp_path):
    r = _check(full_record(prompt_tokens_server=99), tmp_path)
    assert r is not None and r.reason == "tripwire-mismatch"


def test_refuse_null_completion_ids_token_mode(tmp_path):
    rec = full_record(
        completion_token_ids=None,
        completion_ids_source=None,
        completion_logprobs=None,
    )
    r = _check(rec, tmp_path)
    assert r is not None and r.reason == "null-completion-ids"
    assert _check(rec, tmp_path, text_mode=True) is None


def test_ok_record_passes_checks(tmp_path):
    assert _check(full_record(), tmp_path) is None


class StubTok:
    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [100 + i for i in range(len(text.split()))]}


def test_completion_ids_text_mode_fallback(tmp_path):
    recs = load_one(
        tmp_path,
        [
            full_record(
                completion_token_ids=None,
                completion_ids_source=None,
                completion_logprobs=None,
                completion_text_raw="two words",
            )
        ],
    )
    ids, fidelity = completion_ids_for_replay(recs[0], StubTok(), text_mode=True)
    assert fidelity == "text" and ids == [100, 101]


def test_completion_ids_prefer_logged(tmp_path):
    recs = load_one(tmp_path, [full_record()])
    ids, fidelity = completion_ids_for_replay(recs[0], StubTok(), text_mode=True)
    assert fidelity == "ids" and ids == [4, 5]
