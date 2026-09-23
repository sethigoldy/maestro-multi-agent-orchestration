"""Agent processes: odd output bytes, leftover children, large prompts and kills.

Each test reproduces a defect found in review: a run killed by one byte that
is not valid UTF-8, a run that hung (or reported a timeout) because a child
the agent left running held its output open, a large prompt that deadlocked
the pipes, and a group kill that was skipped once the agent itself had exited.

The follow-up tests cover the second review: a child that prints all the time
and so kept the exit grace from starting, a last line without a newline that
was lost, an RPC abort that blocked the run, children that got SIGKILL with no
chance to clean up, a group signalled after it was gone, version probes that
still decoded strictly, and a login-shell snapshot that changed bytes or came
back empty where ``env -0`` does not work.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from maestro.adapters.base import BaseAdapter, _kill_group, _next_line
from maestro.adapters.generic import GenericAdapter
from maestro.agents import AgentSpec


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    # The guard keeps version probes (run in the test's current directory)
    # from doing anything; only a real run passes "--go".
    path = dirpath / name
    path.write_text(f'#!/bin/sh\n[ "$1" = "--go" ] || exit 0\n{body}\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_LOGIN_ENV", "0")
    return bp


def _agent(name: str, input_mode: str = "stdin") -> GenericAdapter:
    return GenericAdapter(AgentSpec(name=name, kind="generic", command=f"{name} --go", input_mode=input_mode))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - pid reused by another user's process
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_output_that_is_not_utf8_does_not_kill_the_run(binpath, tmp_path):
    _fake_bin(binpath, "latin", "cat > /dev/null\nprintf 'caf\\351\\n'\nsleep 1\necho done\nexit 0")
    result = _agent("latin").run("p", tmp_path, "t", timeout=30, log_dir=tmp_path / "logs")
    assert result.ok is True, result.error
    log = Path(result.output_path).read_text(encoding="utf-8")
    assert "caf�" in log and "done" in log


def test_a_background_child_holding_the_output_does_not_hang_the_run(binpath, tmp_path):
    pidfile = tmp_path / "child.pid"
    _fake_bin(binpath, "leaver", f"cat > /dev/null\nsleep 30 &\necho $! > {pidfile}\necho finished\nexit 0")
    started = time.monotonic()
    result = _agent("leaver").run("p", tmp_path, "t", timeout=20)
    assert result.ok is True and result.exit_code == 0, result.error
    assert time.monotonic() - started < 10  # the exit grace, not the child's 30 s
    assert _wait_dead(int(pidfile.read_text().strip()))  # the leftover child was stopped


def test_a_detached_child_is_stopped_after_a_successful_run(binpath, tmp_path):
    pidfile = tmp_path / "child.pid"
    _fake_bin(binpath, "detacher", f"cat > /dev/null\nsleep 30 > /dev/null 2>&1 &\necho $! > {pidfile}\nexit 0")
    result = _agent("detacher").run("p", tmp_path, "t", timeout=20)
    assert result.ok is True
    assert _wait_dead(int(pidfile.read_text().strip()))


def test_a_large_prompt_does_not_deadlock_a_chatty_agent(binpath, tmp_path):
    # The agent prints 200 KB before reading its 300 KB prompt: both pipes fill
    # unless the prompt is written while the output is being read.
    _fake_bin(binpath, "chatty", "head -c 200000 /dev/zero | tr '\\0' 'x'\necho\ncat > /dev/null\necho read-it\nexit 0")
    started = time.monotonic()
    result = _agent("chatty").run("y" * 300_000, tmp_path, "t", timeout=20, log_dir=tmp_path / "logs")
    assert result.ok is True, result.error
    assert time.monotonic() - started < 15
    assert "read-it" in Path(result.output_path).read_text(encoding="utf-8")


def test_kill_group_reaches_children_after_the_agent_exited(tmp_path):
    # The group leader exits (and is not reaped yet) while its child runs on.
    # os.getpgid(leader) fails in that state, which used to skip the kill.
    process = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo $!; exit 0"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    child = int(process.stdout.readline().strip())
    time.sleep(0.3)
    _kill_group(process)
    assert _wait_dead(child)
    process.stdout.close()


def test_kill_group_escalates_to_sigkill(tmp_path):
    process = subprocess.Popen(
        ["/bin/sh", "-c", "trap '' TERM; echo ready; while :; do sleep 0.1; done"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    assert process.stdout.readline().strip() == "ready"
    started = time.monotonic()
    _kill_group(process)
    assert process.poll() == -signal.SIGKILL
    assert time.monotonic() - started < 15
    process.stdout.close()


def test_next_line_reports_idle():
    import queue

    q: queue.Queue = queue.Queue()
    assert _next_line(q, None, None, lambda: True) == ("idle", None)
    calls = []
    assert _next_line(q, time.monotonic() + 0.8, None, lambda: calls.append(1) is not None) == ("timeout", None)
    assert calls  # idle was polled while waiting


class _SlowRpc(BaseAdapter):
    """An rpc agent that never reads its stdin, so a large start command blocks."""

    kind = "slowrpc"
    mode = "rpc"

    def binary(self):
        return "slowrpc"

    def build_command(self, prompt, workspace, task_id, settings):
        return ["slowrpc", "--go"]

    def rpc_start_command(self, prompt, task_id):
        return {"type": "prompt", "message": prompt}

    def rpc_abort_command(self):
        return {"type": "abort"}

    def rpc_event(self, event):
        return {}


def test_rpc_cancel_does_not_interleave_the_abort_with_a_blocked_start(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30")
    started = time.monotonic()
    result = _SlowRpc(AgentSpec(name="slowrpc", kind="generic")).run(
        "z" * 1_000_000, tmp_path, "t", timeout=60, should_cancel=lambda: True,
    )
    assert result.ok is False and "canceled" in (result.error or "")
    assert time.monotonic() - started < 20


def test_rpc_agent_that_exits_while_a_child_holds_the_output(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30 &\nexit 0")
    started = time.monotonic()
    result = _SlowRpc(AgentSpec(name="slowrpc", kind="generic")).run("p", tmp_path, "t", timeout=60)
    assert result.ok is False and "closed its stream before settling" in (result.error or "")
    assert time.monotonic() - started < 15


# -- review follow-ups ------------------------------------------------------


def test_a_child_that_keeps_printing_does_not_keep_a_finished_run_open(binpath, tmp_path):
    # The child prints every 0.2 s, more often than the 0.5 s poll. The exit
    # grace must still start when the agent exits, and what the child prints
    # during the grace must still be recorded.
    pidfile = tmp_path / "child.pid"
    _fake_bin(binpath, "ticker", f"cat > /dev/null\n( while :; do echo tick; sleep 0.2; done ) &\necho $! > {pidfile}\necho agent-finished\nexit 0")
    started = time.monotonic()
    result = _agent("ticker").run("p", tmp_path, "t", timeout=20, log_dir=tmp_path / "logs")
    elapsed = time.monotonic() - started
    assert result.ok is True and result.exit_code == 0, result.error
    assert elapsed < 10  # the two-second grace, not the 20 s timeout
    log = Path(result.output_path).read_text(encoding="utf-8")
    assert "agent-finished" in log and log.count("tick") >= 3
    assert _wait_dead(int(pidfile.read_text().strip()))


def test_the_last_line_without_a_newline_is_kept_after_the_exit_grace(binpath, tmp_path):
    # A background child holds the output open, so the run ends through the
    # exit grace. The agent's last line has no trailing newline; it must still
    # reach the log, the question detection and the error tail.
    question = '{"question": "Should I delete the old tables?"}'
    _fake_bin(binpath, "partial", f"cat > /dev/null\nsleep 30 &\necho first-line\nprintf '%s' '{question}'\nexit 3")
    spec = AgentSpec(name="partial", kind="generic", command="partial --go", input_mode="stdin", output_format="jsonl")
    seen: list[str] = []
    result = GenericAdapter(spec).run("p", tmp_path, "t", timeout=20, log_dir=tmp_path / "logs", on_line=seen.append)
    assert result.ok is False and result.exit_code == 3
    assert result.question == "Should I delete the old tables?"
    assert question in (result.error or "")
    assert Path(result.output_path).read_text(encoding="utf-8") == f"first-line\n{question}"
    assert seen == ["first-line", question]


class _EventRpc(_SlowRpc):
    """An rpc agent whose events are {"type": "done"} and {"type": "fail"}."""

    def rpc_event(self, event):
        if event.get("type") == "done":
            return {"done": True}
        if event.get("type") == "fail":
            return {"fail": event.get("message", "")}
        return {}


def _rpc(name: str = "slowrpc") -> _EventRpc:
    return _EventRpc(AgentSpec(name=name, kind="generic"))


def test_rpc_last_line_without_a_newline_reaches_the_error_tail(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30 &\necho starting\nprintf 'last words'\nexit 0")
    result = _rpc().run("p", tmp_path, "t", timeout=20, log_dir=tmp_path / "logs")
    assert result.ok is False and "closed its stream before settling" in (result.error or "")
    assert result.error.endswith("starting\nlast words")
    assert Path(result.output_path).read_text(encoding="utf-8") == "starting\nlast words"


def test_rpc_done_event_without_a_newline_settles_the_run(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30 &\nprintf '{\"type\": \"done\"}'\nexit 0")
    result = _rpc().run("p", tmp_path, "t", timeout=20)
    assert result.ok is True, result.error


def test_rpc_fail_event_without_a_newline_fails_the_run(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30 &\nprintf '{\"type\": \"fail\", \"message\": \"boom\"}'\nexit 0")
    result = _rpc().run("p", tmp_path, "t", timeout=20, log_dir=tmp_path / "logs")
    assert result.ok is False and result.error == "boom"
    assert result.output_path is not None


def test_rpc_lines_after_done_are_logged_but_do_not_change_the_result(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30 &\necho '{\"type\": \"done\"}'\nprintf '{\"type\": \"fail\", \"message\": \"late\"}'\nexit 0")
    result = _rpc().run("p", tmp_path, "t", timeout=20, log_dir=tmp_path / "logs")
    assert result.ok is True, result.error
    assert "late" in Path(result.output_path).read_text(encoding="utf-8")


class _HugeAbortRpc(_SlowRpc):
    """The abort command is larger than any pipe buffer, so writing it blocks
    for as long as the agent does not read its stdin."""

    def rpc_abort_command(self):
        return {"type": "abort", "pad": "x" * 1_000_000}


def test_rpc_abort_that_cannot_be_written_does_not_block_the_run(binpath, tmp_path, monkeypatch):
    import threading

    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_RPC_CANCEL_GRACE_S", 1.0, raising=False)
    pidfile = tmp_path / "agent.pid"
    _fake_bin(binpath, "slowrpc", f"echo $$ > {pidfile}\nsleep 30")
    outcome: dict = {}

    def _go():
        outcome["result"] = _HugeAbortRpc(AgentSpec(name="slowrpc", kind="generic")).run(
            "p", tmp_path, "t", timeout=60, should_cancel=pidfile.exists,
        )

    started = time.monotonic()
    runner = threading.Thread(target=_go, daemon=True)
    runner.start()
    runner.join(20)
    try:
        assert "result" in outcome, "the run blocked writing the abort command"
        assert outcome["result"].ok is False and "canceled" in (outcome["result"].error or "")
        assert time.monotonic() - started < 15
        assert _wait_dead(int(pidfile.read_text().strip()))
    finally:
        try:
            os.killpg(int(pidfile.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


_CLEANUP_CHILD = (
    "( trap 'sleep 0.3; rm -f {lock}; exit 0' TERM; touch {lock}; touch {ready}; "
    "while :; do sleep 0.05; done ) > /dev/null 2>&1 &\n"
)


def test_a_cancel_lets_children_clean_up_before_sigkill(binpath, tmp_path):
    lock, ready = tmp_path / "index.lock", tmp_path / "ready"
    _fake_bin(binpath, "worker", "cat > /dev/null\n" + _CLEANUP_CHILD.format(lock=lock, ready=ready) + "wait")
    result = _agent("worker").run("p", tmp_path, "t", timeout=20, should_cancel=ready.exists)
    assert result.ok is False and "canceled" in (result.error or "")
    assert not lock.exists()  # the child got SIGTERM and time to remove its lock


def test_leftovers_after_a_successful_run_get_sigterm_first(binpath, tmp_path):
    lock, ready = tmp_path / "index.lock", tmp_path / "ready"
    body = "cat > /dev/null\n" + _CLEANUP_CHILD.format(lock=lock, ready=ready)
    body += f"while [ ! -f {ready} ]; do sleep 0.05; done\nexit 0"
    _fake_bin(binpath, "worker", body)
    result = _agent("worker").run("p", tmp_path, "t", timeout=20)
    assert result.ok is True, result.error
    assert not lock.exists()


def test_a_leftover_that_ignores_sigterm_is_killed_after_the_grace(binpath, tmp_path):
    pidfile = tmp_path / "child.pid"
    _fake_bin(binpath, "stubborn", f"cat > /dev/null\n( trap '' TERM; while :; do sleep 0.05; done ) > /dev/null 2>&1 &\necho $! > {pidfile}\nexit 0")
    started = time.monotonic()
    result = _agent("stubborn").run("p", tmp_path, "t", timeout=20)
    assert result.ok is True, result.error
    assert _wait_dead(int(pidfile.read_text().strip()), timeout=1.0)
    assert 2.0 <= time.monotonic() - started < 10  # the two-second grace, then SIGKILL


class _FakeProc:
    pid = 987654

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def test_a_group_that_is_gone_is_never_signalled(monkeypatch):
    # Once the agent's group is empty its id may be reused by an unrelated
    # process. Maestro checks first and never signals a group it saw gone.
    import maestro.adapters.base as base_mod

    calls: list[tuple[int, int]] = []
    state = {"exists": False}

    def _killpg(pgid, sig):
        calls.append((pgid, sig))
        if not state["exists"]:
            raise ProcessLookupError("no such group")

    monkeypatch.setattr(base_mod.os, "killpg", _killpg)
    proc = _FakeProc()
    base_mod._kill_leftovers(proc)
    assert calls == [(987654, 0)]  # checked, not signalled
    state["exists"] = True  # the id now names someone else's group
    base_mod._kill_leftovers(proc)
    base_mod._kill_group(proc)
    assert calls == [(987654, 0)]


def test_a_group_owned_by_someone_else_is_never_signalled(monkeypatch):
    import maestro.adapters.base as base_mod

    calls: list[int] = []

    def _killpg(pgid, sig):
        calls.append(sig)
        raise PermissionError("not our group")

    monkeypatch.setattr(base_mod.os, "killpg", _killpg)
    base_mod._kill_group(_FakeProc())
    assert calls == [0]


def _latin_binary(path: Path) -> str:
    path.write_text("#!/bin/sh\nprintf 'caf\\351 1.0\\n'\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_version_probes_replace_bytes_that_are_not_utf8(tmp_path, monkeypatch):
    from maestro import agents, doctor

    binary = _latin_binary(tmp_path / "latinver")
    assert agents._probe_version(binary) == "caf� 1.0"
    assert doctor._probe_binary_version(binary) == "caf� 1.0"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _latin_binary(bindir / "git")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert doctor._git_info() == {"installed": True, "version": "caf� 1.0"}


def test_login_env_values_pass_to_agents_byte_for_byte(tmp_path, monkeypatch):
    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_LOGIN_ENV_CACHE", None)
    monkeypatch.delenv("MAESTRO_LOGIN_ENV", raising=False)
    shell = tmp_path / "shell"
    shell.write_text("#!/bin/sh\nexport LATIN_DIR=\"$(printf '/data/caf\\351')\"\nexport CRLF=\"$(printf 'a\\r\\nb')\"\nexec /bin/sh -c \"$2\"\n", encoding="utf-8")
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))
    env = base_mod.capture_login_env()
    assert os.fsencode(env["LATIN_DIR"]) == b"/data/caf\xe9"
    assert env["CRLF"] == "a\r\nb"


@pytest.mark.parametrize("env_zero", [
    "echo 'env: illegal option -- 0' >&2; exit 1",  # an env without -0
    "exit 0",  # an env that accepts -0 but prints nothing
])
def test_login_env_falls_back_to_plain_env(tmp_path, monkeypatch, capsys, env_zero):
    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_LOGIN_ENV_CACHE", None)
    monkeypatch.delenv("MAESTRO_LOGIN_ENV", raising=False)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_env = fakebin / "env"
    fake_env.write_text(f"#!/bin/sh\ncase \"$1\" in -0) {env_zero};; esac\nexec /usr/bin/env \"$@\"\n", encoding="utf-8")
    fake_env.chmod(0o755)
    runs = tmp_path / "runs"
    shell = tmp_path / "shell"
    shell.write_text(f"#!/bin/sh\necho run >> {runs}\nexport PATH={fakebin}:$PATH\nexport API_KEY=from-profile\necho banner\nexec /bin/sh -c \"$2\"\n", encoding="utf-8")
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))
    env = base_mod.capture_login_env()
    assert env["API_KEY"] == "from-profile"
    assert "banner" not in env
    err = capsys.readouterr().err
    assert err.count("warning") == 1 and "multi-line values may be cut" in err
    assert runs.read_text().count("run") == 1  # one login shell, not two


def _login_shell(tmp_path: Path, body: str) -> Path:
    """A fake login shell: records each run in "runs", runs body, then the command."""
    shell = tmp_path / "shell"
    shell.write_text(f"#!/bin/sh\necho run >> {tmp_path / 'runs'}\n{body}\nexec /bin/sh -c \"$2\"\n", encoding="utf-8")
    shell.chmod(0o755)
    return shell


def _env_without_zero(tmp_path: Path) -> Path:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_env = fakebin / "env"
    fake_env.write_text("#!/bin/sh\ncase \"$1\" in -0) echo 'env: illegal option -- 0' >&2; exit 1;; esac\nexec /usr/bin/env \"$@\"\n", encoding="utf-8")
    fake_env.chmod(0o755)
    return fakebin


def test_login_env_fallback_works_with_a_profile_slower_than_half_the_timeout(tmp_path, monkeypatch, capsys):
    # The profile takes 2 s of a 4 s limit. A second login shell for the
    # fallback would get less than 2 s and time out, leaving the snapshot empty.
    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_LOGIN_ENV_CACHE", None)
    monkeypatch.delenv("MAESTRO_LOGIN_ENV", raising=False)
    fakebin = _env_without_zero(tmp_path)
    shell = _login_shell(tmp_path, f"sleep 2\nexport PATH={fakebin}:$PATH\nexport API_KEY=from-profile")
    monkeypatch.setenv("SHELL", str(shell))
    env = base_mod.capture_login_env(timeout_s=4.0)
    assert env.get("API_KEY") == "from-profile"
    assert (tmp_path / "runs").read_text().count("run") == 1
    assert "multi-line values may be cut" in capsys.readouterr().err


@pytest.mark.parametrize("shell_body", [
    "exit 1",  # like SHELL=/usr/bin/false or /sbin/nologin
    "exit 0",  # a profile that exits before the command runs
])
def test_login_env_without_the_marker_is_not_blamed_on_env(tmp_path, monkeypatch, capsys, shell_body):
    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_LOGIN_ENV_CACHE", None)
    monkeypatch.delenv("MAESTRO_LOGIN_ENV", raising=False)
    monkeypatch.setenv("SHELL", str(_login_shell(tmp_path, shell_body)))
    assert base_mod.capture_login_env() == {}
    err = capsys.readouterr().err
    assert err.count("warning") == 1
    assert "could not capture the login environment" in err and "env -0" not in err
    assert (tmp_path / "runs").read_text().count("run") == 1  # not run a second time


def test_login_env_that_prints_nothing_is_reported(tmp_path, monkeypatch, capsys):
    # Both env -0 and plain env print nothing: nothing can be captured.
    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_LOGIN_ENV_CACHE", None)
    monkeypatch.delenv("MAESTRO_LOGIN_ENV", raising=False)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "env").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fakebin / "env").chmod(0o755)
    monkeypatch.setenv("SHELL", str(_login_shell(tmp_path, f"export PATH={fakebin}:$PATH")))
    assert base_mod.capture_login_env() == {}
    assert "could not capture the login environment" in capsys.readouterr().err


def test_login_env_timeout_is_reported(tmp_path, monkeypatch, capsys):
    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_LOGIN_ENV_CACHE", None)
    monkeypatch.delenv("MAESTRO_LOGIN_ENV", raising=False)
    monkeypatch.setenv("SHELL", str(_login_shell(tmp_path, "sleep 5")))
    assert base_mod.capture_login_env(timeout_s=0.3) == {}
    err = capsys.readouterr().err
    assert err.count("warning") == 1 and "could not capture the login environment" in err


class _IgnoresAbortRpc(_SlowRpc):
    """Reads and ignores every command, including the abort."""

    def build_command(self, prompt, workspace, task_id, settings):
        return ["readrpc", "--go"]


def test_rpc_agent_that_ignores_the_abort_is_stopped_when_the_grace_ends(binpath, tmp_path, monkeypatch):
    # The agent reads its input (so the abort is written) but ignores it, and
    # keeps running after stdin closes. It must be stopped when the cancel
    # grace ends, not after a further wait for it to exit on its own.
    import maestro.adapters.base as base_mod

    monkeypatch.setattr(base_mod, "_RPC_CANCEL_GRACE_S", 1.0)
    _fake_bin(binpath, "readrpc", "echo started\nwhile read l; do echo \"got $l\"; done\nsleep 30")
    canceled_at: list[float] = []

    def _cancel():
        if not canceled_at:
            canceled_at.append(time.monotonic())
        return True

    result = _IgnoresAbortRpc(AgentSpec(name="readrpc", kind="generic")).run(
        "p", tmp_path, "t", timeout=60, log_dir=tmp_path / "logs", should_cancel=_cancel,
    )
    assert result.ok is False and "canceled" in (result.error or "")
    assert time.monotonic() - canceled_at[0] < 4  # the 1 s grace plus the group stop, not 1 s + 5 s
    assert '"abort"' in Path(result.output_path).read_text(encoding="utf-8")  # the abort was delivered
