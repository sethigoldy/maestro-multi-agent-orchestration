"""CLI-level tests: ``maestro skill`` commands and ``agents register-discovered``."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from maestro import cli
from maestro.integrations import SKILL_NAME


def _fake_executable(path: Path, body: str = "echo ok") -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Runner:
    def __init__(self, monkeypatch, home: Path, bindir: Path | None = None):
        self.monkeypatch = monkeypatch
        self.home = home
        self.bindir = bindir
        monkeypatch.setenv("MAESTRO_HOME", str(home))  # state dir
        monkeypatch.setenv("HOME", str(home))  # skill install root (Path.home())
        if bindir is not None:
            monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    def run(self, *argv: str) -> tuple[int, str, str]:
        import sys

        import io
        from contextlib import redirect_stdout, redirect_stderr

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


def test_skill_list(tmp_path, monkeypatch, capsys):
    runner = Runner(monkeypatch, tmp_path / "home")
    rc, out, _ = runner.run("skill", "list")
    data = json.loads(out)
    assert rc == 0
    assert data["skill"] == SKILL_NAME
    assert Path(data["source"]).is_file()
    assert len(data["supported_agents"]) == 9


def test_skill_status_all_absent(tmp_path, monkeypatch, capsys):
    runner = Runner(monkeypatch, tmp_path / "home", tmp_path / "bin")
    (tmp_path / "bin").mkdir()
    rc, out, _ = runner.run("skill", "status")
    entries = json.loads(out)
    assert rc == 0 and len(entries) == 9
    assert all(e["installed"] is False for e in entries)
    kinds = {e["kind"] for e in entries}
    assert kinds == {"codex", "claude_code", "copilot", "cursor", "hermes", "pi", "cline", "opencode", "openhands"}


def test_skill_install_agent_flag(tmp_path, monkeypatch, capsys):
    runner = Runner(monkeypatch, tmp_path / "home")  # no binaries detected
    rc, out, _ = runner.run("skill", "install", "--agent", "claude")
    assert rc == 0
    assert "Claude Code" in out and "installed" in out
    target = tmp_path / "home" / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    assert target.is_file()


def test_skill_install_all(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    runner = Runner(monkeypatch, home)
    rc, out, _ = runner.run("skill", "install", "--all")
    assert rc == 0
    assert (home / ".codex" / "instructions.md").is_file()
    assert (home / ".cursor" / "rules" / f"{SKILL_NAME}.mdc").is_file()
    assert (home / ".claude" / "skills" / SKILL_NAME / "SKILL.md").is_file()
    data = json.loads((home / ".openhands" / "agent_settings.json").read_text(encoding="utf-8"))
    assert "maestro-driven-development:begin" in data["custom_instructions"]


def test_skill_install_detected_only(tmp_path, monkeypatch, capsys):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    # Deterministic detection regardless of what the host machine has installed.
    import maestro.integrations as integrations_mod

    monkeypatch.setattr(
        integrations_mod.shutil, "which",
        lambda name: str(bindir / name) if (bindir / name).is_file() else None,
    )
    runner = Runner(monkeypatch, home, bindir)
    rc, out, _ = runner.run("skill", "install")
    assert rc == 0
    assert "✓ Codex" in out and "installed" in out
    assert "not-detected" in out  # the other seven are reported, not failed
    assert (home / ".codex" / "instructions.md").is_file()
    assert not (home / ".claude" / "skills").exists()


def test_skill_install_unknown_agent(tmp_path, monkeypatch, capsys):
    runner = Runner(monkeypatch, tmp_path / "home")
    rc, out, _ = runner.run("skill", "install", "--agent", "vim")
    assert rc == 1 and "unknown or unsupported" in out


def test_skill_install_and_uninstall_roundtrip(tmp_path, monkeypatch, capsys):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    runner = Runner(monkeypatch, home, bindir)

    rc, out, _ = runner.run("skill", "install")
    assert rc == 0 and (home / ".codex" / "instructions.md").is_file()

    rc, out, _ = runner.run("skill", "uninstall")
    assert rc == 0 and "Codex" in out and "uninstalled" in out
    assert not (home / ".codex" / "instructions.md").exists()

    # Idempotent: uninstalling again is a no-op success.
    rc, out, _ = runner.run("skill", "uninstall")
    assert rc == 0 and out.strip() == ""


def test_skill_uninstall_agent_flag(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    runner = Runner(monkeypatch, home)
    runner.run("skill", "install", "--agent", "pi")
    rc, out, _ = runner.run("skill", "uninstall", "--agent", "pi")
    assert rc == 0 and "Pi" in out
    assert not (home / ".pi" / "instructions.md").exists()


def test_skill_uninstall_unknown_agent(tmp_path, monkeypatch, capsys):
    runner = Runner(monkeypatch, tmp_path / "home")
    rc, out, _ = runner.run("skill", "uninstall", "--agent", "emacs")
    assert rc == 1 and "unknown or unsupported" in out


def test_skill_install_rejects_agent_and_all(tmp_path, monkeypatch, capsys):
    runner = Runner(monkeypatch, tmp_path / "home")
    import sys

    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with redirect_stderr(err):
        try:
            cli.main(["skill", "install", "--agent", "codex", "--all"])
            raised = False
        except SystemExit as exc:  # argparse errors exit(2)
            raised = exc.code == 2
    assert raised


# ------------------------------------------------------- agents register-discovered

def test_register_discovered_registers_and_preserves(tmp_path, monkeypatch, capsys):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    _fake_executable(bindir / "claude")
    runner = Runner(monkeypatch, home, bindir)

    # Pre-existing custom registration must survive.
    rc, out, _ = runner.run("agents", "add", "--name", "codex", "--kind", "codex", "--display-name", "My Codex")
    assert rc == 0

    rc, out, _ = runner.run("agents", "register-discovered")
    report = json.loads(out)
    assert rc == 0
    by_name = {r["name"]: r for r in report}
    assert by_name["codex"]["action"] == "preserved"
    assert by_name["claude_code"]["action"] == "registered"

    # The custom codex registration is intact; claude_code got a default spec.
    rc, out, _ = runner.run("agents", "list")
    listed = {a["name"]: a for a in json.loads(out)}
    assert listed["codex"]["display_name"] == "My Codex" and listed["codex"]["source"] == "user"
    assert listed["claude_code"]["source"] == "discovered"

    # Rerun: idempotent, everything preserved.
    rc, out, _ = runner.run("agents", "register-discovered")
    report = json.loads(out)
    assert all(r["action"] == "preserved" for r in report)


def test_register_discovered_dry_run(tmp_path, monkeypatch, capsys):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    runner = Runner(monkeypatch, home, bindir)

    rc, out, _ = runner.run("agents", "register-discovered", "--dry-run")
    report = json.loads(out)
    assert rc == 0 and report[0]["action"] == "would-register"
    assert not (home / "agents" / "codex.toml").exists()


def test_skill_install_result_without_detail(tmp_path, monkeypatch, capsys):
    """Output formatting must tolerate results with no detail text."""
    from maestro import cli
    from maestro.integrations import IntegrationResult, SkillManager

    Runner(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(
        SkillManager,
        "install",
        lambda self, agent=None, all_agents=False: [IntegrationResult("codex", "Codex", True, "installed")],
    )
    rc = cli.main(["skill", "install"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "✓ Codex            installed" in out and "(" not in out.splitlines()[0]


def test_skill_uninstall_result_without_detail(tmp_path, monkeypatch, capsys):
    from maestro import cli
    from maestro.integrations import IntegrationResult, SkillManager

    Runner(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(
        SkillManager,
        "uninstall",
        lambda self, agent=None: [IntegrationResult("codex", "Codex", True, "skipped")],
    )
    rc = cli.main(["skill", "uninstall"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "✓ Codex            skipped" in out and "(" not in out.splitlines()[0]


def test_format_daemon_info_minimal():
    """_format_daemon_info must render a bare DaemonInfo (no pid/url/state/uptime)."""
    from maestro import cli
    from maestro.daemonctl import DaemonInfo

    text = cli._format_daemon_info(DaemonInfo(running=False), "stopped")
    assert text == "Maestro daemon stopped"
