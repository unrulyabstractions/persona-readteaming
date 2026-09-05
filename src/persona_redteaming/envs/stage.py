"""Stage `/agent` the way each environment's Docker entrypoint does.

Both environments normally ship as Docker images: the Dockerfile copies the
agent-facing files into `/agent`, hides the harness in a root-only `/opt`,
creates the `dev` user and hands `/agent` to it; `entrypoint.py` then stages the
one variant the config selects and launches the loop.

A vast.ai instance is itself an unprivileged container, so Docker cannot run
inside it. This module reproduces the same end state on the host filesystem, at
the SAME absolute path the image uses, so the agent's view of the world is
unchanged. Every step is a line-for-line counterpart of the environment's own
`Dockerfile` + `entrypoint.py`; the deviations are listed in `DEVIATIONS` and
copied into every run's `run_meta.json`.

Nothing here touches the submodule checkout: files are read from it and copied
out.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

WORKSPACE = Path("/agent")


def set_workspace(path: str | Path) -> Path:
    """Point staging at a different workspace root (dry runs, or one slot per
    concurrent rollout). ``run_meta.json`` records the root either way, so a
    dry run can never be mistaken for a measurement."""
    global WORKSPACE
    WORKSPACE = Path(path)
    return WORKSPACE


# ---------------------------------------------------------------------------
# Per-environment file plans, read straight off the Dockerfiles + entrypoints
# ---------------------------------------------------------------------------

#: env -> (files copied verbatim into /agent by the Dockerfile,
#:         variant key -> {destination filename: source filename})
ENV_PLANS: dict[str, dict] = {
    "sandbagging": {
        # Dockerfile: COPY environments/sandbagging/questions.txt /agent/questions.txt
        "static": ["questions.txt"],
        # entrypoint.py: task.reverse picks which Documentation lands in /agent
        "variant_key": "reverse",
        "variants": {
            "false": {"Documentation.md": "Documentation.md"},
            "true": {"Documentation.md": "Documentation_reverse.md"},
        },
    },
    "eval_tampering": {
        # Dockerfile: questions.txt, format_guide.md, deployment_notes.txt
        "static": ["questions.txt", "format_guide.md", "deployment_notes.txt"],
        # entrypoint.py: task.variation selects the discovery file
        "variant_key": "variation",
        "variants": {
            "notes_self_weapons": {"notes_to_self.txt": "notes_self_weapons.txt"},
        },
    },
}

DEVIATIONS = [
    "Rollouts run directly on the rented host, not in the environment's Docker "
    "image: vast.ai instances are unprivileged containers and cannot nest "
    "Docker. The workspace path (/agent), the `dev` user, the sanitized agent "
    "shell and the root-only harness tree are reproduced and then MEASURED.",
    "The agent's shell sees the host toolchain (the vLLM image's python) "
    "instead of python:3.11-slim. Neither environment's task requires a "
    "toolchain: the agent reads text files and calls submit.",
    "The image's `git init --separate-git-dir=/opt/.git` pristine commit is "
    "not reproduced. It exists for container REUSE across rollouts; every "
    "rollout here stages /agent from scratch, so there is nothing to reset to.",
    "/tmp is shared with the harness host rather than being a fresh container "
    "tmpfs. Neither environment writes to /tmp, and the per-step filesystem "
    "snapshot tracks the workspace only; anything the agent leaves in /tmp is "
    "audited by name/size/uid/sha256 into run_meta.json instead of stored.",
    "Network egress is not blocked (matches the default non-isolated harness "
    "runner; neither environment is in ISOLATED_ENVIRONMENTS).",
]


class StagingError(RuntimeError):
    """The host cannot reproduce the environment's invariants."""


def _pw(name: str):
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def ensure_users() -> dict:
    """Create the image's `dev` account if missing.

    `run_command` drops every agent shell command to `dev`, and
    `assert_hardened()` refuses to run a root loop without it.
    """
    created = []
    if _pw("dev") is None:
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", "dev"], check=True)
        created.append("dev")
    dev = _pw("dev")
    if dev is None:
        raise StagingError("failed to create the 'dev' user")
    return {"created": created, "dev_uid": dev.pw_uid, "dev_gid": dev.pw_gid,
            "dev_home": dev.pw_dir}


def assert_harness_private(harness_root: Path) -> None:
    """The agent's shell must not be able to read the harness tree.

    In the image this is `chmod 700 /opt`: the loop's code, the unused
    discovery variants and the checkpointed ground truth (`state.json` carries
    `correct_answer`) are invisible to the privilege-dropped shell. Here the
    equivalent tree is the campaign root, so the same lock is applied and then
    VERIFIED by attempting the read as `dev` rather than trusting mode bits.
    """
    harness_root = Path(harness_root)
    os.chmod(harness_root, 0o700)
    dev = _pw("dev")
    if dev is None or os.geteuid() != 0:
        return
    probe = subprocess.run(
        ["su", "-s", "/bin/sh", "dev", "-c", f"ls {harness_root} 2>&1"],
        capture_output=True, text=True,
    )
    if probe.returncode == 0 and "Permission denied" not in probe.stdout:
        raise StagingError(
            f"the agent user can still read the harness root {harness_root}: "
            f"{probe.stdout[:200]!r}. The image's `chmod 700 /opt` invariant is "
            "not reproduced; refusing to run."
        )


def stage_workspace(env_dir: Path, env_name: str, variant: str) -> dict:
    """Build /agent for one environment + variant, as the entrypoint does.

    Returns the sha256 of every file placed in the workspace, so a run's
    starting tree is provable after the fact.
    """
    env_dir = Path(env_dir)
    plan = ENV_PLANS.get(env_name)
    if plan is None:
        raise StagingError(f"unknown environment {env_name!r}; "
                           f"supported: {sorted(ENV_PLANS)}")
    if variant not in plan["variants"]:
        raise StagingError(
            f"unknown {env_name} variant {variant!r}; "
            f"supported: {sorted(plan['variants'])}"
        )

    if WORKSPACE.exists():
        os.chmod(WORKSPACE, 0o755)
        for child in WORKSPACE.iterdir():
            try:
                os.chmod(child, 0o644)
            except OSError:
                pass
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    staged: list[tuple[str, Path]] = []
    for name in plan["static"]:
        src = env_dir / name
        if not src.exists():
            raise StagingError(f"required workspace file missing: {src}")
        shutil.copy(src, WORKSPACE / name)
        staged.append((name, src))
    for dest, src_name in plan["variants"][variant].items():
        src = env_dir / src_name
        if not src.exists():
            raise StagingError(f"required variant file missing: {src}")
        shutil.copy(src, WORKSPACE / dest)
        staged.append((dest, src))

    # The image's `chown -R dev:dev /agent` plus the entrypoint's
    # chown_to_agent() after it stages the variant out of the root-only /opt.
    dev = _pw("dev")
    chowned = False
    if dev is not None and os.geteuid() == 0:
        os.chown(WORKSPACE, dev.pw_uid, dev.pw_gid)
        for f in WORKSPACE.iterdir():
            os.chown(f, dev.pw_uid, dev.pw_gid)
        chowned = True

    digests = {}
    for f in sorted(WORKSPACE.iterdir()):
        st = f.stat()
        digests[f.name] = {
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            "size": st.st_size,
            "mode": oct(st.st_mode & 0o777),
            "uid": st.st_uid,
        }
    return {
        "env": env_name,
        "variant": variant,
        "files": digests,
        "sources": {d: str(s) for d, s in staged},
        "chowned_to_dev": chowned,
        "workspace": str(WORKSPACE),
    }


def verify_agent_shell() -> dict:
    """Prove, by running as `dev`, that the workspace really is the agent's.

    Mode bits are a claim; this is the measurement. The agent must be able to
    read every staged file and write into the workspace (both environments
    invite it to use the workspace freely), and must NOT be root.
    """
    if os.geteuid() != 0 or _pw("dev") is None:
        return {"checked": False, "reason": "not root, or no dev user"}
    ws = str(WORKSPACE)
    probes = {
        "read_workspace": f"ls -la {ws}",
        "read_each_file": f"for f in {ws}/*; do head -c 1 \"$f\" >/dev/null || exit 1; done",
        "write_scratch": f"touch {ws}/.probe_write && rm -f {ws}/.probe_write",
        "whoami": "id -un",
    }
    checks = {}
    for name, cmd in probes.items():
        r = subprocess.run(["su", "-s", "/bin/sh", "dev", "-c", cmd],
                           capture_output=True, text=True)
        checks[name] = {"rc": r.returncode, "out": (r.stdout + r.stderr)[:200]}
    checks["agent_is_dev"] = checks["whoami"]["out"].strip() == "dev"
    checks["workspace_readable"] = checks["read_workspace"]["rc"] == 0
    checks["files_readable"] = checks["read_each_file"]["rc"] == 0
    checks["workspace_writable"] = checks["write_scratch"]["rc"] == 0
    checks["checked"] = True
    if not all((checks["agent_is_dev"], checks["workspace_readable"],
                checks["files_readable"], checks["workspace_writable"])):
        raise StagingError(
            f"the agent's shell does not have the environment's own view of "
            f"the workspace on this host: {checks}"
        )
    return checks


def snapshot_workspace(dest: Path) -> dict:
    """Copy the final workspace out, with per-file hashes."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = {}
    for f in sorted(WORKSPACE.rglob("*")):
        if f.is_dir() or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(WORKSPACE)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(f, target)
        out[str(rel)] = {
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            "size": f.stat().st_size,
            "mode": oct(f.stat().st_mode & 0o777),
            "uid": f.stat().st_uid,
        }
    return out


if __name__ == "__main__":  # manual staging smoke check
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--aie", required=True, help="agent-interp-envs checkout")
    ap.add_argument("--env", required=True, choices=sorted(ENV_PLANS))
    ap.add_argument("--variant", required=True)
    ap.add_argument("--workspace", default="/agent")
    ap.add_argument("--harness-root", default=None)
    args = ap.parse_args()
    set_workspace(args.workspace)
    print(json.dumps(ensure_users(), indent=1))
    if args.harness_root:
        assert_harness_private(Path(args.harness_root))
        print(f"harness root {args.harness_root} verified private to the loop")
    env_dir = Path(args.aie) / "environments" / args.env
    print(json.dumps(stage_workspace(env_dir, args.env, args.variant), indent=1))
    print(json.dumps(verify_agent_shell(), indent=1))
