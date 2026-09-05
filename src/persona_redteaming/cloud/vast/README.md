# vast.ai lifecycle for one vLLM campaign box

Six scripts, one config file per campaign, no per-run copies. CLOUD.md at the
repo root is binding for all of it; this file documents only the interface.

| File | Role |
|---|---|
| `lib.sh` | shared helpers: config resolution, secret redaction, three-state instance truth, all-pages pagination, label ownership + foreign blacklist, box-identity assertion, cost guard |
| `01_launch.sh` | offer search by ranked GPU tiers, create, ssh reachability, watchdog start |
| `02_provision.sh` | ship the package and the aie fork, deliver `HF_TOKEN` by stdin, `pip install -e` both into the IMAGE's python (law 2 asserted), download the pinned model, fingerprint, start vLLM, health-wait |
| `04_capture.sh` | byte-verified sweep of every root, no extension filter |
| `05_destroy.sh` | destroy, gated on the capture marker, verified gone across all pages |
| `watchdog.sh` | cost/time cap with heartbeat grace; captures and destroys on its own if the cap trips |
| `configs/*.env` | presets: `qwen3-32b`, `r1-distill-qwen-14b`, `qwen3-32b-controls` |

## Configuration

`QC_CONFIG` names the campaign and is required. A bare name resolves to
`configs/<name>.env`; a path is used as is. Every value in the file is
overridable from the shell environment. State and captures live next to the
config file (`.state/<WORKSTREAM>/`, `capture/<WORKSTREAM>/`), so the package
directory stays clean and two campaigns never share a state directory.

The knobs that change between campaigns:

| Knob | Meaning |
|---|---|
| `WORKSTREAM` / `INSTANCE_LABEL` | one box, one owner (law 8); every script refuses a box whose live label differs |
| `FOREIGN_INSTANCES`, `FOREIGN_LABELS` | hard refusals; both must be non-empty |
| `IMAGE`, `IMAGE_CUDA_MIN` | prebuilt vLLM image (law 2) and the offer's minimum `cuda_max_good` |
| `GPU_TIERS` | ranked `gpu:min_gpu_ram_gb:price_floor:price_cap` |
| `DISK_GB` | `>= 6 x model_size_GB + 250`; never lowered silently |
| `MODEL_ID`, `MODEL_REVISION`, `SERVED_MODEL_NAME`, `MAX_MODEL_LEN` | what vLLM serves, pinned |
| `MAX_HOURS`, `MAX_DOLLARS`, `WATCHDOG_GRACE_MIN` | the caps |
| `LOCAL_PKG`, `LOCAL_AIE` | the persona-redteaming repo root and the aie submodule (defaults resolve from this directory) |

Secrets: `HF_TOKEN` only, from the local shell env, delivered over ssh stdin
to a 0600 file. No judge runs in a campaign, so no other key is shipped. No
`set -x` anywhere; `lib.sh` turns tracing off if a caller enabled it.

## Command sequence

```
cd src/persona_redteaming/cloud/vast
export QC_CONFIG=qwen3-32b            # or a path to your own .env
YES=1 bash 01_launch.sh
bash 02_provision.sh
# on the box: the campaign
#   cd /workspace/persona-redteaming && python -m persona_redteaming.envs.campaign \
#       --root /workspace/campaign --aie /workspace/aie \
#       --served-model qwen3-32b --tokenizer-model unsloth/Qwen3-32B \
#       --model-revision <sha> --cells main --em-construction turns16num \
#       --concurrency 4 --workspace '/agent-{slot}'
#   python -m persona_redteaming.envs.grade /workspace/campaign/runs --write
#   python -m persona_redteaming.envs.harvest --root /workspace/campaign --cells ... \
#       --base-model unsloth/Qwen3-32B --revision <sha> --layer-chunks 4
#   python -m persona_redteaming.envs.upload --root /workspace/campaign --cells ... \
#       --model-short qwen3-32b
bash 04_capture.sh
bash 05_destroy.sh --marker .state/<WORKSTREAM>/CAPTURE_OK_<id> --yes-i-am-really-sure
```

While the campaign runs: touch `.state/<WORKSTREAM>/heartbeat` at least every
20 minutes (a fresh heartbeat earns the grace window), and write a pid file at
`/workspace/campaign.pid`, `/workspace/driver.pid` or `/workspace/harvest.pid`
for any long process; `04_capture.sh` refuses to sweep while one is alive.

What to check after each step is printed by the step itself and listed in
CLOUD.md. The short version: `02` must print `STACK_BEFORE` equal to
`STACK_AFTER` for torch and vllm, `EDITABLE_INSTALL_FROM` for both trees,
`IMPORT_OK` for the three modules, and `/v1/models` listing exactly
`SERVED_MODEL_NAME`; `04` must print `CAPTURE VERIFIED: ... 0 lost`; `05`
must print `destroyed and VERIFIED gone (all pages walked)`.

## Static checks

```
bash -n lib.sh 01_launch.sh 02_provision.sh 04_capture.sh 05_destroy.sh watchdog.sh configs/*.env
shellcheck -S warning ./*.sh configs/*.env
```

Rotate the vast API key after a campaign: vast writes `/root/.vast_api_key`
on every instance it creates.
