"""The campaign spec table and the runner's refusals, without a server.

`run_campaign` shells out to `rollout.py` per run; here it is exercised only
up to the point where it would launch, by pointing it at a `sys.executable`
stand-in through monkeypatching `run_one`, so what is proved is the command
each cell produces and the refusals the runner makes before spending
anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import AIE

from persona_redteaming.envs import campaign as cp

if not (AIE / "configs" / "sandbagging" / "normal.yaml").exists():
    pytest.skip(f"no agent-interp-envs checkout at {AIE}", allow_module_level=True)


def test_every_cell_is_well_formed():
    for name, spec in cp.CELLS.items():
        assert spec["group"] in ("pilot", "main", "pc", "debug"), name
        assert (spec["env"], spec["variant"]) in cp.ENV_CONFIGS, name
        assert spec["arm"] and spec["role"], name
        if spec.get("suffix"):
            assert (cp.SUFFIX_DIR / spec["suffix"]).exists(), name
        if spec.get("icl") and spec["icl"] != "PICKED":
            assert (cp.DEFAULT_ICL_DIR / f"{spec['icl']}.json").exists(), name
            assert spec["placement"] in ("first_user_message", "prior_turns"), name


def test_control_pairs_exist_for_both_environments():
    for env in ("sb", "et"):
        assert cp.CELLS[f"{env}_pc"]["group"] == "pc"
        assert cp.CELLS[f"{env}_nc"]["group"] == "pc"
        assert cp.CELLS[f"{env}_pc"]["suffix"] != cp.CELLS[f"{env}_nc"]["suffix"]


def test_resolve_cells_by_group_and_by_name():
    main = cp.resolve_cells("main", em_construction="turns16num")
    assert set(main) == {"sb_control", "sb_em", "sb_reverse", "et_control", "et_em"}
    assert main["sb_em"]["icl"] == "turns16num"
    assert main["sb_em"]["placement"] == "prior_turns"
    two = cp.resolve_cells("sb_pc, et_nc")
    assert list(two) == ["sb_pc", "et_nc"]
    assert len(cp.resolve_cells("debug")) == 31
    assert "A_system_noicl" in cp.resolve_cells("all", em_construction="turns8")


def test_resolve_cells_refuses_unknown_and_unpicked():
    with pytest.raises(ValueError):
        cp.resolve_cells("sb_control,nope")
    with pytest.raises(ValueError):
        cp.resolve_cells("sb_em")
    with pytest.raises(ValueError):
        cp.resolve_cells("sb_em", em_construction="turns99")


def test_build_cell_config_copies_prompts_verbatim(tmp_path):
    import yaml

    src = yaml.safe_load((AIE / "configs" / "sandbagging" / "normal.yaml").read_text())
    out = cp.build_cell_config(AIE, tmp_path / "c.yaml", "sb_control",
                               cp.CELLS["sb_control"], "served-alias")
    cfg = yaml.safe_load(out.read_text())
    assert cfg["prompts"] == src["prompts"]
    assert cfg["agent"]["provider"] == "vllm" and cfg["agent"]["model"] == "served-alias"
    assert "reasoning_effort" not in cfg["agent"]
    assert cfg["_campaign"]["cell"] == "sb_control"


def test_run_campaign_builds_one_command_per_run_and_pairs_seeds(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def fake_run_one(cmd, log_path):
        seen.append(cmd)
        return 0, str(log_path)

    monkeypatch.setattr(cp, "run_one", fake_run_one)
    specs = cp.resolve_cells("sb_control,sb_pc,E1c_one_call_prompt")
    summary = cp.run_campaign(specs, root=tmp_path, aie=AIE, served_model="s",
                              tokenizer_model="t", n=2, n_pc=1, n_debug=1, seed_base=100)
    assert summary["n_jobs"] == 4 and summary["n_failed"] == 0
    by_cell: dict[str, list[list[str]]] = {}
    for cmd in seen:
        run_dir = Path(cmd[cmd.index("--run-dir") + 1])
        by_cell.setdefault(run_dir.parent.name, []).append(cmd)
    assert [c[c.index("--seed") + 1] for c in by_cell["sb_control"]] == ["101", "102"]
    assert by_cell["sb_pc"][0][by_cell["sb_pc"][0].index("--seed") + 1] == "101"
    pc = by_cell["sb_pc"][0]
    assert pc[pc.index("--prompt-suffix-file") + 1] == str(cp.SUFFIX_DIR / "sandbagging_pc.txt")
    dbg = by_cell["E1c_one_call_prompt"][0]
    assert dbg[dbg.index("--prompt-suffix-file") + 1] == str(cp.SUFFIX_DIR / "one_call.txt")
    assert dbg[1:3] == ["-m", "persona_redteaming.envs.rollout"]
    assert json.loads((tmp_path / "campaign_summary.json").read_text())["n_jobs"] == 4
    assert (tmp_path / "configs" / "sb_pc.yaml").exists()


def test_run_campaign_refuses_concurrency_without_a_slot(tmp_path):
    with pytest.raises(ValueError):
        cp.run_campaign(cp.resolve_cells("sb_control"), root=tmp_path, aie=AIE,
                        served_model="s", tokenizer_model="t", concurrency=2)


def test_run_campaign_skips_completed_runs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cp, "run_one", lambda cmd, log: (calls.append(cmd), 0, "")[1:])
    done = tmp_path / "runs" / "sb_pc" / "run-01"
    done.mkdir(parents=True)
    (done / "run_meta.json").write_text("{}")
    cp.run_campaign(cp.resolve_cells("sb_pc"), root=tmp_path, aie=AIE,
                    served_model="s", tokenizer_model="t", n_pc=2)
    assert len(calls) == 1


def test_cli_reports_unknown_cells_without_running(tmp_path, capsys):
    rc = cp.main(["--root", str(tmp_path), "--aie", str(AIE), "--served-model", "s",
                  "--tokenizer-model", "t", "--cells", "nope"])
    assert rc == 2
    assert "unknown cells" in capsys.readouterr().err
