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
    elif mode == "followup":
        prompt = f"""You are Codex, the implementation and debugging agent in a Claude-supervised Maestro workflow.

Task ID: {task_id}

Codex model: {model or "Codex default"}
Reasoning effort: {effort or "Codex default"}

AUTHORITATIVE APPROVED DESIGN:
{design}

FOLLOW-UP INSTRUCTION FROM CLAUDE:
{review or "(none)"}

Execute the follow-up fully in the repository. Claude is supervising, so do not ask Claude to perform implementation work. Inspect the current state, make the required code/test changes, run the most relevant tests/checks, and report files changed, commands/results, deviations, and remaining issues.
"""
    else:
        prompt = f"""You are Codex, the primary implementation agent in a Claude-supervised Maestro workflow.

Task ID: {task_id}

Codex model: {model or "Codex default"}
Reasoning effort: {effort or "Codex default"}

AUTHORITATIVE APPROVED DESIGN:
{design}

Implement the approved design in the current repository. Claude is the supervisor and reviewer; do not duplicate implementation work in Claude. Own the implementation, tests, refactoring, and local debugging needed to satisfy the approved acceptance criteria. Run focused tests before reporting back.
"""

    task_dir = m.state_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    from .adapters.codex import probe_codex_autonomy_flags

    codex_cmd = ["codex", "exec", *probe_codex_autonomy_flags()]
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


def _verification_command(root: Path, configured: list[str] | None = None) -> tuple[list[str], str | None]:
    if configured:
        return list(configured), None
    if (root / "Makefile").exists():
        return ["make", "check"], None
    package_json = root / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if isinstance(scripts, dict) and scripts.get("test"):
                if (root / "pnpm-lock.yaml").exists():
                    return ["pnpm", "test"], None
                if (root / "yarn.lock").exists():
                    return ["yarn", "test"], None
                return ["npm", "test"], None
        except (OSError, json.JSONDecodeError):
            pass
    if (root / "go.mod").exists():
        return ["go", "test", "./..."], None
    if (root / "Cargo.toml").exists():
        return ["cargo", "test"], None
    python_root = root / "pyproject.toml"
    pytest_configured = (root / "pytest.ini").exists() or (root / "tox.ini").exists() or (root / "setup.cfg").exists()
    has_tests = (root / "tests").is_dir()
    if python_root.exists() or pytest_configured or has_tests:
        python = _python_executable(root)
        probe = subprocess.run(
            [python, "-c", "import pytest"], cwd=root, text=True, capture_output=True,
        )
        if probe.returncode == 0:
            return [python, "-m", "pytest"], None
        return ["git", "diff", "--check"], "pytest is not installed in the selected Python environment; skipped test runner"
    return ["git", "diff", "--check"], "no project test runner detected; using git diff --check only"


def verify(m: Maestro, task_id: str) -> bool:
    m._write_claim(task_id, "task_status", Phase.VERIFYING.value)
    diff = subprocess.run(["git", "diff", "--check"], cwd=m.root, text=True, capture_output=True)
    configured = m.config.get("verification_command")
    test_cmd, note = _verification_command(m.root, configured)
    tests = subprocess.run(test_cmd, cwd=m.root, text=True, capture_output=True)
    ok = diff.returncode == 0 and tests.returncode == 0
    note_text = f"verification note: {note}\n\n" if note else ""
    report = f"workspace: {m.root}\nverification command: {' '.join(test_cmd)}\n\n{note_text}git diff --check:\n{diff.stdout}\n{diff.stderr}\n\nverification:\n{tests.stdout}\n{tests.stderr}"
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
    p.add_argument("action", choices=["implement", "fix", "followup"])
    p.add_argument("task_id")
    p.add_argument("--review")
    p.add_argument("--workspace", required=True)
    args = p.parse_args()
    m = Maestro(args.workspace)
    try:
        return run_codex(m, m.resolve_task(args.task_id), args.action, args.review)
    finally:
        m.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
