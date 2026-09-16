from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import Maestro
from .models import Phase


def now():
    return datetime.now(timezone.utc)


def design_for(m: Maestro, task_id: str) -> str:
    claims = m.mem.history(m._subject(task_id), "task_design")
    if not claims:
        raise KeyError(task_id)
    return Path(claims[-1].object).read_text(encoding="utf-8")


def _claim_value(m: Maestro, task_id: str, predicate: str) -> str | None:
    claims = m.mem.history(m._subject(task_id), predicate)
    return claims[-1].object if claims else None


def run_codex(m: Maestro, task_id: str, mode: str, review: str | None) -> int:
    design = design_for(m, task_id)
    model = _claim_value(m, task_id, "task_model")
    effort = _claim_value(m, task_id, "task_effort")
    if mode == "fix":
        prompt = f"""You are Codex, the implementation agent in a Claude-supervised Maestro workflow.

Task ID: {task_id}

Codex model: {model or "Codex default"}
Reasoning effort: {effort or "Codex default"}

AUTHORITATIVE APPROVED DESIGN:
{design}

CLAUDE REVIEW FINDINGS TO FIX:
{review or '(none)'}

Fix the existing implementation. Preserve the approved architecture unless the review or repository constraints require a change.
Run focused tests and report files changed, tests/results, deviations, and remaining issues.
"""
    else:
        prompt = f"""You are Codex, the implementation agent in a Claude-supervised Maestro workflow.

Task ID: {task_id}

Codex model: {model or "Codex default"}
Reasoning effort: {effort or "Codex default"}

AUTHORITATIVE APPROVED DESIGN:
{design}

Implement the approved design in the current repository. Do not redesign the feature unless the repository makes the design impossible.
Run focused tests and report files changed, tests/results, deviations, and remaining issues.
"""

    task_dir = m.state_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    codex_cmd = ["codex", "exec", "--full-auto"]
    if model:
        codex_cmd.extend(["--model", model])
    if effort:
        codex_cmd.extend(["--config", f'model_reasoning_effort="{effort}"'])
    codex_cmd.append("-")
    result = subprocess.run(
        codex_cmd,
        cwd=m.root,
        input=prompt,
        text=True,
        capture_output=True,
    )
    payload = {"task_id": task_id, "exit_code": result.returncode,
               "model": model, "effort": effort,
               "stdout": result.stdout, "stderr": result.stderr,
               "finished_at": now().isoformat(), "mode": mode}
    artifact = task_dir / f"codex-{mode}-result.json"
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    episode = m.mem.add(
        f"Codex {mode} result for {task_id}.\n{result.stdout}\n{result.stderr}",
        role="system", ts=now(),
    )
    m._write_claim(task_id, "task_result", str(artifact), episode.episode_ids)
    m._write_claim(task_id, "task_status",
                   Phase.VERIFYING.value if result.returncode == 0 else Phase.FAILED.value,
                   episode.episode_ids)

    if result.returncode == 0:
        verify(m, task_id)
    return result.returncode


def _python_executable(root: Path) -> str:
    """Return the same interpreter that is running Maestro when possible."""
    env_python = os.environ.get("MAESTRO_PYTHON")
    if env_python and Path(env_python).is_file():
        return env_python

    repo_python = root / ".venv" / "bin" / "python"
    if repo_python.is_file() and os.access(repo_python, os.X_OK):
        return str(repo_python)

    return sys.executable


def _verification_command(root: Path) -> list[str]:
    if (root / "Makefile").exists():
        return ["make", "check"]
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tests").exists():
        return [_python_executable(root), "-m", "pytest"]
    return ["git", "diff", "--check"]


def verify(m: Maestro, task_id: str) -> bool:
    m._write_claim(task_id, "task_status", Phase.VERIFYING.value)
    diff = subprocess.run(["git", "diff", "--check"], cwd=m.root, text=True, capture_output=True)
    test_cmd = _verification_command(m.root)
    tests = subprocess.run(test_cmd, cwd=m.root, text=True, capture_output=True)
    ok = diff.returncode == 0 and tests.returncode == 0
    report = f"workspace: {m.root}\nverification command: {' '.join(test_cmd)}\n\ngit diff --check:\n{diff.stdout}\n{diff.stderr}\n\nverification:\n{tests.stdout}\n{tests.stderr}"
    task_dir = m.state_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    report_path = task_dir / "verification.txt"
    report_path.write_text(report, encoding="utf-8")
    episode = m.mem.add(f"Verification for {task_id}.\n{report}", role="system", ts=now())
    m._write_claim(task_id, "task_verification", f"{'PASSED' if ok else 'FAILED'}: {report_path}", episode.episode_ids)
    # Always return to Claude for review. Verification failure is evidence, not an
    # automatic agent decision; Claude decides whether the failure is expected or needs a fix.
    m._write_claim(task_id, "task_status", Phase.REVIEWING.value, episode.episode_ids)
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["implement", "fix"])
    p.add_argument("task_id")
    p.add_argument("--review")
    p.add_argument("--workspace", required=True)
    args = p.parse_args()
    m = Maestro(args.workspace)
    try:
        return run_codex(m, m.resolve_task(args.task_id), args.action, args.review)
    finally:
        m.close()


if __name__ == "__main__":
    raise SystemExit(main())
