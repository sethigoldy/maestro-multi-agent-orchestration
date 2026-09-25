"""Installer tests for install.sh.

Fully hermetic: the script runs as a real subprocess against a fake HOME, a fake
PATH (stub ``python3``/``git`` executables), and a minimal fake repository. The
venv's ``python`` is a symlink to the test interpreter so the installer's inline
JSON parsing runs for real; every ``maestro`` CLI call inside the installer hits
a stateful stub that records what it was asked to do.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

REAL_PY = sys.executable


#: Every interpreter name install.sh's require_python() probes. Fakes must be
#: provided under ALL of them (PATH puts the fake bindir first, so ours always
#: wins — a CI runner may have real python3.12/python3.11 in /usr/bin).
PYTHON_NAMES = ("python3.12", "python3.11", "python3", "python")


def _write(path: Path, body: str, executable: bool = True) -> None:
    path.write_text(body, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_python_fakes(bindir: Path, body: str) -> None:
    for name in PYTHON_NAMES:
        _write(bindir / name, body)


class FakeEnv:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.bindir = tmp_path / "bin"
        self.repo = tmp_path / "repo"
        self.state_dir = tmp_path / "daemon-state"
        self.log = tmp_path / "fake.log"
        for d in (self.home, self.bindir, self.state_dir):
            d.mkdir(parents=True)
        self._build_repo()
        self._build_fakes()

    def _build_repo(self):
        # Minimal stand-in for the Maestro repository (verify_repo's contract).
        (self.repo / "maestro").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "maestro"\nversion = "0.10.0"\n', encoding="utf-8"
        )
        (self.repo / "maestro" / "cli.py").write_text("# fake cli\n", encoding="utf-8")

    def _build_fakes(self):
        # Stateful maestro stub: daemon start/stop toggles a marker file.
        _write(
            self.bindir / "maestro-stub",
            """#!/bin/sh
echo "maestro $*" >> "$FAKE_LOG"
case "$1 $2" in
  "agents register-discovered") echo '[{"name":"codex","action":"registered"}]' ;;
  "skill install") echo "ok skill install" ;;
  "skill uninstall") echo "ok skill uninstall" ;;
  "daemon status")
    if [ -f "$FAKE_STATE_DIR/running" ]; then
      echo '{"running": true, "pid": 4242, "port": 8790, "url": "http://127.0.0.1:8790", "state_dir": "/fake"}'
    else
      echo '{"running": false, "pid": null, "port": null, "url": null}'
    fi ;;
  "daemon start") mkdir -p "$FAKE_STATE_DIR"; touch "$FAKE_STATE_DIR/running"; echo "Maestro daemon started" ;;
  "daemon restart") mkdir -p "$FAKE_STATE_DIR"; touch "$FAKE_STATE_DIR/running"; echo "Maestro daemon restarted" ;;
  "daemon stop") rm -f "$FAKE_STATE_DIR/running"; echo "stopped" ;;
  "agents list") echo '[{"name":"codex","display_name":"Codex"},{"name":"claude_code","display_name":"Claude Code"}]' ;;
  "skill status") echo '[{"kind":"codex","display_name":"Codex","installed":true,"subagent_installed":true,"subagent_path":"/fake/home/.codex/agents/maestro-worker.toml"},{"kind":"claude_code","display_name":"Claude Code","installed":false}]' ;;
  *) echo "maestro-stub" ;;
esac
""",
        )
        # Fake Python (under every probed name): version handshake + venv
        # creation with a real-python shim for the installer's inline JSON.
        _write_python_fakes(
            self.bindir,
            f"""#!/bin/sh
case "$1" in
  --version) echo "Python 3.11.9"; exit 0 ;;
  -c)
    shift
    if [ "$1" = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' ]; then exit 0; fi
    exec "{REAL_PY}" -c "$@"
    ;;
  -m)
    if [ "$2" = "venv" ]; then
      dir="$3"
      mkdir -p "$dir/bin"
      ln -sf "{REAL_PY}" "$dir/bin/python"
      printf '#!/bin/sh\\necho "pip $*" >> "$FAKE_LOG"\\nexit 0\\n' > "$dir/bin/pip"
      for t in maestro maestro-daemon maestro-mcp; do cp "{self.bindir}/maestro-stub" "$dir/bin/$t"; done
      chmod +x "$dir/bin/pip" "$dir/bin/maestro" "$dir/bin/maestro-daemon" "$dir/bin/maestro-mcp"
      exit 0
    fi
    exec "{REAL_PY}" -m "$@"
    ;;
esac
exec "{REAL_PY}" "$@"
""",
        )
        # Fake git: clone copies the fake repo; everything else is a no-op.
        _write(
            self.bindir / "git",
            """#!/bin/sh
echo "git $*" >> "$FAKE_LOG"
if [ "$1" = "clone" ]; then
  dest=""; for a in "$@"; do dest="$a"; done
  cp -R "$FAKE_REPO" "$dest"
  exit 0
fi
exit 0
""",
        )

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": f"{self.bindir}:/usr/bin:/bin",
            "MAESTRO_REPO_URL": str(self.repo),
            "FAKE_LOG": str(self.log),
            "FAKE_REPO": str(self.repo),
            "FAKE_STATE_DIR": str(self.state_dir),
            "TMPDIR": str(self.tmp),
        }

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(INSTALL_SH), *args],
            env=self.env(), capture_output=True, text=True, timeout=120,
        )

    @property
    def log_lines(self) -> list[str]:
        if not self.log.is_file():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()


def test_install_script_syntax():
    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_fresh_install_end_to_end(fake_env: FakeEnv):
    result = fake_env.run()
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout

    # Summary block (feature 11 shape).
    assert "Maestro installed successfully." in out
    assert "✓ running" in out and "PID: 4242" in out and "URL: http://127.0.0.1:8790" in out
    assert "✓ Codex" in out and "✓ Claude Code" in out
    assert "maestro-driven-development" in out
    assert "✓ Codex subagent maestro_worker (/fake/home/.codex/agents/maestro-worker.toml)" in out

    # Install layout (feature 8).
    install_root = fake_env.home / ".local" / "share" / "maestro"
    for name in ("maestro", "maestro-daemon", "maestro-mcp"):
        launcher = fake_env.home / ".local" / "bin" / name
        assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert (install_root / "src" / "pyproject.toml").is_file()
    assert (install_root / "venv" / "bin" / "maestro").is_file()

    # Orchestration order (features 9/10/11): register agents, install skill, start daemon.
    log = fake_env.log_lines
    assert any("maestro agents register-discovered" in line for line in log)
    assert any("maestro skill install" in line for line in log)
    assert any("maestro daemon start" in line for line in log)
    assert not any("maestro daemon restart" in line for line in log)  # fresh install: start, not restart
    assert (fake_env.state_dir / "running").is_file()

    # The launcher actually drives the installed CLI.
    launcher = fake_env.home / ".local" / "bin" / "maestro"
    probe = subprocess.run([str(launcher), "agents", "list"], capture_output=True, text=True, env=fake_env.env())
    assert probe.returncode == 0 and "Codex" in probe.stdout


def test_rerun_is_an_update_not_a_reinstall(fake_env: FakeEnv):
    first = fake_env.run()
    assert first.returncode == 0, first.stdout + first.stderr
    second = fake_env.run()
    assert second.returncode == 0, second.stdout + second.stderr

    log = fake_env.log_lines
    clones = [line for line in log if line.startswith("git clone")]
    fetches = [line for line in log if "fetch" in line]
    assert len(clones) == 1  # second run updated in place
    assert len(fetches) == 1
    # A running daemon is restarted to pick up the new version.
    assert any("maestro daemon restart" in line for line in log)
    # No duplicate orchestration of skill installs beyond one per run.
    assert sum(1 for line in log if "maestro skill install" in line) == 2


def test_uninstall_removes_installation_keeps_state(fake_env: FakeEnv):
    fake_env.run()  # install first
    state_file = fake_env.home / ".maestro" / "keep-me.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}", encoding="utf-8")

    result = fake_env.run("--uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    install_root = fake_env.home / ".local" / "share" / "maestro"
    assert not install_root.exists()
    for name in ("maestro", "maestro-daemon", "maestro-mcp"):
        assert not (fake_env.home / ".local" / "bin" / name).exists()
    assert state_file.is_file()  # user state preserved
    assert any("maestro daemon stop" in line for line in fake_env.log_lines)
    assert any("maestro skill uninstall" in line for line in fake_env.log_lines)


def test_uninstall_purge_state(fake_env: FakeEnv):
    fake_env.run()
    state_file = fake_env.home / ".maestro" / "keep-me.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}", encoding="utf-8")

    result = fake_env.run("--uninstall", "--purge-state")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (fake_env.home / ".maestro").exists()


def test_uninstall_without_prior_install(fake_env: FakeEnv):
    result = fake_env.run("--uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Maestro uninstalled." in result.stdout


def test_missing_git_is_a_clear_error(tmp_path):
    env = FakeEnv(tmp_path)
    (env.bindir / "git").unlink()  # only the fake python3 remains on PATH
    result = subprocess.run(
        ["bash", str(INSTALL_SH)],
        env=env.env(),
        capture_output=True, text=True, timeout=60,
    )
    # /usr/bin may contain git on some systems; only assert the error when it is absent.
    host_has_git = (
        subprocess.run(
            ["bash", "-lc", "command -v git"],
            env={"PATH": f"{env.bindir}:/usr/bin:/bin"}, capture_output=True,
        ).returncode == 0
    )
    if not host_has_git:
        assert result.returncode == 1
        assert "git is required" in result.stderr


def test_python_too_old_is_a_clear_error(tmp_path):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bindir.mkdir()
    # All probed names must be too old, or a real system python3.1x (present on
    # CI runners) would satisfy the check first.
    _write_python_fakes(
        bindir,
        """#!/bin/sh
case "$1" in
  --version) echo "Python 3.9.0"; exit 0 ;;
  -c) exit 1 ;;
esac
exit 1
""",
    )
    _write(bindir / "git", "#!/bin/sh\nexit 0\n")
    result = subprocess.run(
        ["bash", str(INSTALL_SH)],
        env={"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert "Python >= 3.11 is required" in result.stderr


def test_unknown_option_rejected(fake_env: FakeEnv):
    result = fake_env.run("--bogus")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


@pytest.fixture()
def fake_env(tmp_path) -> FakeEnv:
    return FakeEnv(tmp_path)
