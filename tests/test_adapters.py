from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from maestro.adapters import AdapterNotAvailable, BaseAdapter, ClaudeCodeAdapter, ClineAdapter, CodexAdapter, CopilotAdapter, CursorAdapter, GenericAdapter, HermesAdapter, OpenHandsAdapter, PiAdapter, make_adapter
from maestro.agents import AgentSpec


def _fake_bin(dirpath: Path, name: str, body: str) -> Path:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _spec(name: str, kind: str = "codex", **kw) -> AgentSpec:
    return AgentSpec(name=name, kind=kind, **kw)


# Real CLIs answer `exec --help` instantly; fakes must too, because the codex
# adapter probes its flag surface before building the run command.
_HELP_GUARD = 'if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then echo usage; exit 0\nfi\n'


# ---------------------------------------------------------------- preflight
def test_preflight_ok(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", "echo 'codex-cli 0.9'")
    import os

    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        result = CodexAdapter(_spec("codex")).preflight()
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.binary == "codex" and result.version == "codex-cli 0.9"


def test_preflight_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = CodexAdapter(_spec("codex")).preflight()
    assert result.ok is False and "not found" in (result.error or "")


def test_preflight_no_binary_known():
    adapter = _NoBinaryAdapter()
    assert adapter.preflight().ok is False


class _NoBinaryAdapter(CodexAdapter):
    def binary(self):
        return None


def test_preflight_auth_probe_failure(monkeypatch, tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", "echo 'codex-cli 0.9'")
    monkeypatch.setenv("PATH", f"{binpath}:{os.environ['PATH']}")

    class _Probed(CodexAdapter):
        def auth_probe(self):
            return [sys.executable, "-c", "import sys; sys.exit(1)"]

    result = _Probed(_spec("codex")).preflight()
    assert result.ok is False and "not authenticated" in (result.error or "")


def test_preflight_auth_probe_ok(monkeypatch, tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", "echo 'codex-cli 0.9'")
    monkeypatch.setenv("PATH", f"{binpath}:{os.environ['PATH']}")

    class _Probed(CodexAdapter):
        def auth_probe(self):
            return [sys.executable, "-c", "pass"]

    result = _Probed(_spec("codex")).preflight()
    assert result.ok is True


# ------------------------------------------------------- command construction
_CODEX_FAKE_OLD_HELP = r"""
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
    echo "Usage: codex exec [OPTIONS]"
    echo "  --full-auto   autonomous execution (old surface)"
    exit 0
fi
cat > /dev/null
exit 0
"""

_CODEX_FAKE_NEW_HELP = r"""
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
    echo "Usage: codex exec [OPTIONS]"
    echo "  -s, --sandbox <MODE>   sandbox policy (new surface)"
    exit 0
fi
cat > /dev/null
exit 0
"""


def _codex_adapter_with_help(tmp_path, monkeypatch, help_body):
    binpath = tmp_path / "bin"
    binpath.mkdir(exist_ok=True)
    _fake_bin(binpath, "codex", help_body)
    monkeypatch.setenv("PATH", f"{binpath}{os.pathsep}{os.environ['PATH']}")


def test_codex_command_old_flag_surface(tmp_path, monkeypatch):
    _codex_adapter_with_help(tmp_path, monkeypatch, _CODEX_FAKE_OLD_HELP)
    adapter = CodexAdapter(_spec("codex", model="gpt-x", effort="max"))
    cmd = adapter.build_command("prompt", tmp_path, "task-1", {})
    assert cmd[:3] == ["codex", "exec", "--full-auto"]
    assert ["--model", "gpt-x"] in [cmd[i : i + 2] for i in range(len(cmd) - 1)]
    assert any('model_reasoning_effort="max"' in part for part in cmd)
    assert cmd[-1] == "-"


def test_codex_command_new_flag_surface(tmp_path, monkeypatch):
    _codex_adapter_with_help(tmp_path, monkeypatch, _CODEX_FAKE_NEW_HELP)
    adapter = CodexAdapter(_spec("codex"))
    cmd = adapter.build_command("p", tmp_path, "t", {})
    assert cmd[:3] == ["codex", "exec", "--approve-for-me"]
    # probe result is cached per instance: a second build does not re-probe
    assert adapter._autonomy_flags == ["--approve-for-me"]


def test_codex_command_no_cli_visible(tmp_path, monkeypatch):
    # No codex on PATH at all: the current flag surface is assumed.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    adapter = CodexAdapter(_spec("codex"))
    cmd = adapter.build_command("p", tmp_path, "t", {})
    assert cmd[:3] == ["codex", "exec", "--approve-for-me"]


def test_codex_command_task_settings_override_spec(tmp_path, monkeypatch):
    _codex_adapter_with_help(tmp_path, monkeypatch, _CODEX_FAKE_NEW_HELP)
    adapter = CodexAdapter(_spec("codex", model="spec-model"))
    cmd = adapter.build_command("p", tmp_path, "t", {"model": "task-model", "effort": "low"})
    assert "task-model" in cmd and "spec-model" not in cmd


def test_claude_command_and_usage_parsing(tmp_path):
    adapter = ClaudeCodeAdapter(_spec("cc", kind="claude_code"))
    cmd = adapter.build_command("p", tmp_path, "t", {"model": "sonnet"})
    assert cmd[:2] == ["claude", "-p"] and "--output-format" in cmd and "stream-json" in cmd
    line = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.42, "duration_ms": 1234})
    parsed = adapter.parse_line(line)
    assert parsed == {"usage": {"cost_usd": 0.42, "duration_ms": 1234}}
    assert adapter.parse_line("plain text") is None
    assert adapter.parse_line("{broken json") is None


def test_codex_usage_parsing():
    adapter = CodexAdapter(_spec("codex"))
    parsed = adapter.parse_line(json.dumps({"type": "turn.completed", "total_cost_usd": 1.5, "tokens_used": 99}))
    assert parsed == {"usage": {"cost_usd": 1.5, "tokens": 99}}
    usage_obj = adapter.parse_line(json.dumps({"usage": {"input_tokens": 10, "output_tokens": 20}}))
    assert usage_obj == {"usage": {"input_tokens": 10, "output_tokens": 20}}
    assert adapter.parse_line("not json") is None


# ------------------------------------------------------------------ factory
def test_factory_kinds():
    assert isinstance(make_adapter(_spec("codex")), CodexAdapter)
    assert isinstance(make_adapter(_spec("cc", kind="claude_code")), ClaudeCodeAdapter)
    assert isinstance(make_adapter(_spec("g", kind="generic", command="x")), GenericAdapter)
    assert isinstance(make_adapter(_spec("pi", kind="pi")), PiAdapter)
    assert isinstance(make_adapter(_spec("cl", kind="cline")), ClineAdapter)
    assert isinstance(make_adapter(_spec("hm", kind="hermes")), HermesAdapter)
    assert isinstance(make_adapter(_spec("cu", kind="cursor")), CursorAdapter)
    assert isinstance(make_adapter(_spec("oh", kind="openhands")), OpenHandsAdapter)
    assert isinstance(make_adapter(_spec("cp", kind="copilot")), CopilotAdapter)
    with pytest.raises(AdapterNotAvailable):
        make_adapter(_spec("xx", kind="carrier-pigeon"))


# ---------------------------------------------------------------- generic
def test_generic_requires_command():
    with pytest.raises(ValueError):
        GenericAdapter(None)  # type: ignore[arg-type]


def test_generic_arg_mode_renders_prompt(tmp_path):
    spec = _spec("g", kind="generic", command="echo {prompt} in {workspace}")
    adapter = GenericAdapter(spec)
    cmd = adapter.build_command("hello world", tmp_path, "t1", {})
    assert cmd == ["echo", "hello world", "in", str(tmp_path)]


def test_generic_stdin_mode_keeps_template(tmp_path):
    spec = _spec("g", kind="generic", command="cat > /dev/null; echo done", input_mode="stdin")
    adapter = GenericAdapter(spec)
    cmd = adapter.build_command("ignored", tmp_path, "t1", {})
    assert "ignored" not in " ".join(cmd)


def test_generic_jsonl_question_and_usage(tmp_path):
    spec = _spec("g", kind="generic", command="x", output_format="jsonl")
    adapter = GenericAdapter(spec)
    parsed = adapter.parse_line(json.dumps({"question": "which db?", "cost_usd": 0.5}))
    assert parsed == {"question": "which db?", "usage": {"cost_usd": 0.5}}
    assert adapter.parse_line("plain") is None
    text_mode = GenericAdapter(_spec("g2", kind="generic", command="x"))
    assert text_mode.parse_line(json.dumps({"question": "q"})) is None


# ------------------------------------------------------------------ spawn run
def test_spawn_success_streams_output_and_logs(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho line1\necho line2')
    import os

    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        adapter = CodexAdapter(_spec("codex"))
        lines: list[str] = []
        result = adapter.run("prompt", tmp_path, "task-1", log_dir=tmp_path / "logs", on_line=lines.append)
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.exit_code == 0
    assert lines == ["line1", "line2"]
    log = Path(result.output_path)
    assert log.is_file() and "line1" in log.read_text(encoding="utf-8")


def test_spawn_failure_captures_tail(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho boom 1>&2\nexit 3')
    import os

    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        result = CodexAdapter(_spec("codex")).run("prompt", tmp_path, "task-1")
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and result.exit_code == 3
    assert "code 3" in (result.error or "") and "boom" in (result.error or "")


def test_spawn_timeout_kills_process(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", _HELP_GUARD + 'cat > /dev/null\nsleep 30')
    import os

    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        start = time.monotonic()
        result = CodexAdapter(_spec("codex")).run("prompt", tmp_path, "task-1", timeout=1)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "timed out" in (result.error or "")
    assert time.monotonic() - start < 20


def test_spawn_cancel_flag_kills_process(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", _HELP_GUARD + 'cat > /dev/null\ni=0\nwhile [ $i -lt 100 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
    import os

    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        state = {"cancel": False}
        start = time.monotonic()
        result = CodexAdapter(_spec("codex")).run(
            "prompt", tmp_path, "task-1",
            should_cancel=lambda: state["cancel"],
            on_line=lambda line: state.update(cancel=True) if line == "tick" else None,
        )
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "canceled" in (result.error or "")
    assert time.monotonic() - start < 10


def test_spawn_launch_failure(tmp_path):
    class _Bad(CodexAdapter):
        def build_command(self, prompt, workspace, task_id, settings):
            return ["/nonexistent/binary/xyz"]

    result = _Bad(_spec("codex")).run("p", tmp_path, "t")
    assert result.ok is False and "Failed to launch" in (result.error or "")


def test_unimplemented_mode_raises(tmp_path):
    class _Api(CodexAdapter):
        mode = "api"

    # api mode is implemented (v2-M3); without a base URL it fails cleanly
    # instead of raising AdapterNotAvailable.
    result = _Api(_spec("x")).run("p", tmp_path, "t")
    assert result.ok is False and "base URL" in (result.error or "")


def test_unknown_mode_still_raises(tmp_path):
    class _Weird(CodexAdapter):
        mode = "telepathy"

    with pytest.raises(AdapterNotAvailable):
        _Weird(_spec("x")).run("p", tmp_path, "t")


def test_generic_run_end_to_end(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "mycli", "cat > /dev/null\necho '{\"cost_usd\": 0.1}'\necho done")
    import os

    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        spec = _spec("g", kind="generic", command="mycli --run {prompt}", output_format="jsonl")
        result = GenericAdapter(spec).run("hello", tmp_path, "task-9")
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.usage == {"cost_usd": 0.1}


# ------------------------------------------------------- coverage gap closure
def test_preflight_to_dict(tmp_path, monkeypatch):
    result = CodexAdapter(_spec("codex")).preflight()
    data = result.to_dict()
    assert set(data) == {"ok", "binary", "version", "error"} and isinstance(data["ok"], bool)


def test_auth_probe_launch_failure(monkeypatch, tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", "echo 'codex-cli 0.9'")
    monkeypatch.setenv("PATH", f"{binpath}:{os.environ['PATH']}")

    class _Probed(CodexAdapter):
        def auth_probe(self):
            return ["/nonexistent/probe-binary"]

    result = _Probed(_spec("codex")).preflight()
    assert result.ok is False and "Auth probe failed to run" in (result.error or "")


def test_base_adapter_defaults():
    from maestro.adapters.base import BaseAdapter

    base = BaseAdapter(_spec("codex"))
    with pytest.raises(NotImplementedError):
        base.build_command("p", Path("/tmp"), "t", {})
    assert base.parse_line("anything") is None
    assert base.detect_question("anything") is None


def test_spawn_stdout_closes_but_process_lingers(tmp_path):
    # A python fake that closes only its stdout fd and then lingers: the pipe
    # EOFs while the process lives past the 5s grace window.
    binpath = tmp_path / "bin"
    binpath.mkdir()
    script = binpath / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        'if len(sys.argv) >= 3 and sys.argv[1] == "exec" and sys.argv[2] == "--help":\n'
        '    print("usage")\n'
        "    sys.exit(0)\n"
        "sys.stdin.read()\n"
        'print("done")\n'
        "sys.stdout.flush()\n"
        "os.close(1)\n"
        "os.close(2)  # stderr is a dup of stdout (stderr=STDOUT): close it too\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        start = time.monotonic()
        result = CodexAdapter(_spec("codex")).run("prompt", tmp_path, "task-1")
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "did not exit" in (result.error or "")
    assert time.monotonic() - start < 20


def test_kill_group_escalates_to_sigkill(tmp_path):
    # A process that traps SIGTERM must be reaped with SIGKILL.
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", _HELP_GUARD + 'cat > /dev/null\ntrap "" TERM\necho alive\nsleep 30')
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        start = time.monotonic()
        result = CodexAdapter(_spec("codex")).run("prompt", tmp_path, "task-1", timeout=8)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "timed out" in (result.error or "")
    assert time.monotonic() - start < 25


def test_probe_version_empty_and_unreadable(tmp_path, monkeypatch):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "silent", "exit 0")
    (binpath / "adir").mkdir()  # a directory on PATH: exec fails with OSError
    monkeypatch.setenv("PATH", f"{binpath}:{os.environ['PATH']}")
    from maestro.adapters.base import _probe_version

    assert _probe_version(str(binpath / "silent")) is None
    assert _probe_version(str(binpath / "adir")) is None


def test_claude_parse_branches(tmp_path):
    adapter = ClaudeCodeAdapter(_spec("cc", kind="claude_code"))
    assert adapter.parse_line(json.dumps([1, 2])) is None  # non-dict JSON
    cost_only = json.dumps({"type": "result", "total_cost_usd": 0.5})
    assert adapter.parse_line(cost_only) == {"usage": {"cost_usd": 0.5}}
    duration_only = json.dumps({"type": "result", "duration_ms": 100})
    assert adapter.parse_line(duration_only) == {"usage": {"duration_ms": 100}}
    empty_result = json.dumps({"type": "result"})
    assert adapter.parse_line(empty_result) is None
    other_type = json.dumps({"type": "assistant", "message": {}})
    assert adapter.parse_line(other_type) is None


def test_codex_parse_branches():
    adapter = CodexAdapter(_spec("codex"))
    assert adapter.parse_line("plain text line") is None
    assert adapter.parse_line("{broken") is None
    assert adapter.parse_line(json.dumps([1, 2])) is None
    empty_usage = json.dumps({"usage": {"weird_key": 5}})
    assert adapter.parse_line(empty_usage) is None


def test_generic_parse_branches():
    spec = _spec("g", kind="generic", command="x", output_format="jsonl")
    adapter = GenericAdapter(spec)
    assert adapter.parse_line(json.dumps([1, 2])) is None  # non-dict JSON event
    assert adapter.parse_line(json.dumps({"unrelated": True})) is None  # no cost/question


def test_spawn_stdin_write_failure_swallowed(monkeypatch, tmp_path):
    import maestro.adapters.base as base_mod

    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", "echo ok\nexit 0")  # never reads stdin
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"

    real_popen = base_mod.subprocess.Popen

    class _BrokenStdin:
        def __init__(self, real):
            self._real = real

        def write(self, data):
            raise OSError("pipe broken")

        def close(self):
            self._real.close()

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _Popen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if self.stdin is not None:
                self.stdin = _BrokenStdin(self.stdin)

    monkeypatch.setattr(base_mod.subprocess, "Popen", _Popen)
    try:
        result = CodexAdapter(_spec("codex")).run("prompt", tmp_path, "task-1")
    finally:
        os.environ["PATH"] = old
        monkeypatch.undo()
    assert result.ok is True  # the broken write is swallowed; the run still succeeds


def test_kill_group_sigkill_lookup_error_swallowed(monkeypatch, tmp_path):
    import maestro.adapters.base as base_mod

    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", _HELP_GUARD + 'cat > /dev/null\ntrap "" TERM\necho alive\nsleep 30')
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"

    real_killpg = base_mod.os.killpg
    seen: list[int] = []

    def _killpg(pgid, sig):
        seen.append(sig)
        if sig == signal.SIGKILL:
            raise ProcessLookupError("already gone")
        real_killpg(pgid, sig)

    monkeypatch.setattr(base_mod.os, "killpg", _killpg)
    try:
        result = CodexAdapter(_spec("codex")).run("prompt", tmp_path, "task-1", timeout=6)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and signal.SIGKILL in seen


def test_kill_group_term_lookup_error_swallowed(monkeypatch):
    # The group may already be gone by the time we send SIGTERM (race on slow
    # teardown); the lookup error must be swallowed, not raised. Deterministic
    # across OSes — no real process involved.
    import maestro.adapters.base as base_mod

    class _Proc:
        pid = 987654

        def wait(self, timeout=None):
            return 0

    seen: list[int] = []

    def _killpg(pgid, sig):
        seen.append(sig)
        raise ProcessLookupError("group already gone")

    monkeypatch.setattr(base_mod.os, "getpgid", lambda pid: 12345)
    monkeypatch.setattr(base_mod.os, "killpg", _killpg)
    base_mod._kill_group(_Proc())  # must not raise
    assert seen == [signal.SIGTERM]


def test_spawn_streaming_oserror(monkeypatch, tmp_path):
    import maestro.adapters.base as base_mod

    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "codex", "echo ok\nexit 0")
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"

    real_popen = base_mod.subprocess.Popen

    class _BadWait(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._broken_once = True

        def wait(self, timeout=None):
            if self._broken_once:  # first wait (the streaming grace window) fails
                self._broken_once = False
                raise OSError("stream broke")
            return real_popen.wait(self, timeout)

    monkeypatch.setattr(base_mod.subprocess, "Popen", _BadWait)
    try:
        result = CodexAdapter(_spec("codex")).run("prompt", tmp_path, "task-1")
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "failed while streaming output" in (result.error or "")


# ---------------------------------------------------------------- pi (rpc)
_PI_FAKE_PY = """
import json, os, sys

behavior = BEHAVIOR  # injected by _pi_adapter

def send(obj):
    print(json.dumps(obj), flush=True)

if behavior == "settle":
    import time
    time.sleep(0.3)
    send({"type": "response", "command": "prompt", "success": True})
    send({"type": "agent_settled"})
    sys.exit(0)

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        cmd = json.loads(raw)
    except Exception:
        continue
    t = cmd.get("type")
    if t == "prompt":
        if behavior == "reject":
            send({"type": "response", "command": "prompt", "success": False, "error": "nope"})
            sys.exit(0)
        if behavior == "retryfail":
            send({"type": "response", "command": "prompt", "success": True})
            send({"type": "auto_retry_end", "finalError": "529 overloaded_error: Overloaded"})
            sys.exit(1)
        send({"type": "response", "command": "prompt", "success": True, "id": cmd.get("id")})
        if behavior == "eof":
            sys.exit(0)  # stream closes before the agent settles
        if behavior == "hang":
            continue  # stay alive and silent; base must time out or cancel us
        send({"type": "agent_start"})
        send({"type": "message_update", "usage": {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 1, "totalTokens": 18, "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.003}}})
        send({"type": "message_update", "usage": {}})
        send({"type": "message_update", "usage": "weird"})
        send({"type": "auto_retry_end"})
        print("garbage non-json line", flush=True)
        send("[1, 2]")
        send({"type": "agent_end", "messages": []})
        send({"type": "agent_settled"})
    elif t == "abort":
        with open(os.environ["PI_ABORT_MARKER"], "w") as fh:
            fh.write("aborted")
        if os.environ.get("PI_ABORT_SETTLES", "exit") == "settle":
            send({"type": "agent_settled"})
        elif os.environ.get("PI_ABORT_SETTLES", "exit") == "silence":
            continue  # stay alive and silent; grace must expire
        sys.exit(0)
# stdin EOF -> exit 0
"""


def _pi_adapter(tmp_path, behavior="ok"):
    binpath = tmp_path / "bin"
    binpath.mkdir(exist_ok=True)
    (binpath / "_pi_fake.py").write_text(f"BEHAVIOR = {behavior!r}\n" + _PI_FAKE_PY, encoding="utf-8")
    _fake_bin(binpath, "pi", 'exec python3 "$(dirname "$0")/_pi_fake.py" "$@"')
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    return old


def test_pi_happy_path(tmp_path):
    import maestro.adapters.base as base_mod

    old = _pi_adapter(tmp_path)
    try:
        seen: list[str] = []
        result = PiAdapter(_spec("pi", kind="pi")).run(
            "do the thing", tmp_path, "task-1", timeout=30, log_dir=tmp_path / "logs",
            on_line=seen.append,
        )
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.exit_code == 0
    assert result.usage == {
        "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 2,
        "cache_write_tokens": 1, "total_tokens": 18, "cost_usd": 0.003,
    }
    log = (tmp_path / "logs" / "pi-task-1.log").read_text(encoding="utf-8")
    assert '"agent_settled"' in log and "garbage non-json line" in log
    assert any("agent_settled" in line for line in seen)  # on_line saw the stream


def test_pi_build_command_model(tmp_path):
    adapter = PiAdapter(_spec("pi", kind="pi", model="anthropic/claude-sonnet-4"))
    cmd = adapter.build_command("p", tmp_path, "t", {})
    assert cmd == ["pi", "--mode", "rpc", "--model", "anthropic/claude-sonnet-4"]
    plain = PiAdapter(_spec("pi", kind="pi")).build_command("p", tmp_path, "t", {})
    assert plain == ["pi", "--mode", "rpc"]


def test_pi_prompt_rejected(tmp_path):
    old = _pi_adapter(tmp_path, behavior="reject")
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "rejected the prompt" in (result.error or "") and "nope" in result.error


def test_pi_retry_final_error(tmp_path):
    old = _pi_adapter(tmp_path, behavior="retryfail")
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "gave up after automatic retries" in (result.error or "")


def test_pi_timeout(tmp_path):
    old = _pi_adapter(tmp_path, behavior="hang")
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run("p", tmp_path, "t", timeout=2)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "timed out" in (result.error or "")


def test_pi_stream_closes_before_settle(tmp_path):
    old = _pi_adapter(tmp_path, behavior="eof")
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "closed its stream before settling" in (result.error or "")


def test_pi_cancel_settles_during_grace(tmp_path):
    marker = tmp_path / "abort-marker"
    old = _pi_adapter(tmp_path, behavior="hang")
    monkeypatch_env = {"PI_ABORT_MARKER": str(marker), "PI_ABORT_SETTLES": "settle"}
    for k, v in monkeypatch_env.items():
        os.environ[k] = v
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run(
            "p", tmp_path, "t", timeout=30, should_cancel=lambda: True
        )
    finally:
        os.environ["PATH"] = old
        for k in monkeypatch_env:
            del os.environ[k]
    assert result.ok is False and "was canceled" in (result.error or "")
    assert marker.read_text(encoding="utf-8") == "aborted"


def test_pi_cancel_grace_expires(tmp_path):
    old = _pi_adapter(tmp_path, behavior="hang")
    os.environ["PI_ABORT_MARKER"] = str(tmp_path / "abort-marker")
    os.environ["PI_ABORT_SETTLES"] = "silence"
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run(
            "p", tmp_path, "t", timeout=30, should_cancel=lambda: True
        )
    finally:
        os.environ["PATH"] = old
        del os.environ["PI_ABORT_MARKER"]
        del os.environ["PI_ABORT_SETTLES"]
    assert result.ok is False and "was canceled" in (result.error or "")


def test_pi_launch_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = PiAdapter(_spec("pi", kind="pi")).run("p", tmp_path, "t", timeout=5)
    assert result.ok is False and "Failed to launch" in (result.error or "")


def test_pi_broken_stdin_and_wait(monkeypatch, tmp_path):
    import subprocess

    import maestro.adapters.base as base_mod

    old = _pi_adapter(tmp_path, behavior="settle")
    real_popen = base_mod.subprocess.Popen

    class _BrokenRpcStdin:
        def __init__(self, real):
            self._real = real

        def write(self, data):
            raise OSError("pipe broken")

        def flush(self):
            pass

        def close(self):
            raise ValueError("already closed")

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _Popen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if self.stdin is not None:
                self.stdin = _BrokenRpcStdin(self.stdin)

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout)

    monkeypatch.setattr(base_mod.subprocess, "Popen", _Popen)
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
    assert result.ok is True  # broken writes and wait are swallowed; settled wins


# ---------------------------------------------------------------- cline
_CLINE_FAKE = r"""
cat > /dev/null
echo '{"type":"say","text":"working on it","ts":1,"say":"text"}'
echo '{"type":"ask","text":"proceed?","ts":2,"ask":"followup"}'
echo '{"type":"say","text":"done: file created","ts":3,"say":"text"}'
exit ${CLINE_RC:-0}
"""


def test_cline_happy_path(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "cline", _CLINE_FAKE)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        result = ClineAdapter(_spec("cl", kind="cline")).run(
            "make a file", tmp_path, "task-1", timeout=30, log_dir=tmp_path / "logs"
        )
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.exit_code == 0
    assert result.question is None  # headless auto-approve: asks never block
    log = (tmp_path / "logs" / "cline-task-1.log").read_text(encoding="utf-8")
    assert '"say":"text"' in log and '"ask":"followup"' in log


def test_cline_failure_tail(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "cline", _CLINE_FAKE)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    os.environ["CLINE_RC"] = "3"
    try:
        result = ClineAdapter(_spec("cl", kind="cline")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
        del os.environ["CLINE_RC"]
    assert result.ok is False and "exited with code 3" in (result.error or "")
    assert "done: file created" in result.error


def test_cline_build_command_flags(tmp_path):
    adapter = ClineAdapter(_spec("cl", kind="cline", model="gpt-5", effort="high"))
    cmd = adapter.build_command("p", tmp_path, "t", {})
    assert cmd == ["cline", "--json", "--model", "gpt-5", "--thinking", "high"]
    plain = ClineAdapter(_spec("cl", kind="cline")).build_command("p", tmp_path, "t", {"effort": "max"})
    assert plain == ["cline", "--json"]  # "max" is not a cline thinking level


# ---------------------------------------------------------------- hermes
_HERMES_FAKE_PY = """
import json, os, sys

args = sys.argv[1:]
prompt = None
usage_file = None
i = 0
while i < len(args):
    a = args[i]
    if a == "-z":
        prompt = args[i + 1]
        i += 2
        continue
    if a == "--usage-file":
        usage_file = args[i + 1]
        i += 2
        continue
    i += 1

print(f"final answer to: {prompt}")
mode = os.environ.get("HERMES_USAGE_MODE", "full")
if usage_file and mode != "nowrite":
    if mode == "garbage":
        with open(usage_file, "w") as fh:
            fh.write("not json")
    elif mode == "partial":
        with open(usage_file, "w") as fh:
            fh.write(json.dumps({"estimated_cost_usd": 0.42}))
    elif mode == "tokens_only":
        with open(usage_file, "w") as fh:
            fh.write(json.dumps({"input_tokens": 5}))
    elif mode == "empty":
        with open(usage_file, "w") as fh:
            fh.write(json.dumps({}))
    else:
        report = {
            "estimated_cost_usd": 0.42, "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 7, "cache_write_tokens": 3, "reasoning_tokens": 9,
            "total_tokens": 150, "model": "test-model", "provider": "test",
            "completed": True, "failed": False,
        }
        with open(usage_file, "w") as fh:
            fh.write(json.dumps(report))
sys.exit(int(os.environ.get("HERMES_RC", "0")))
"""


def _hermes_adapter(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir(exist_ok=True)
    (binpath / "_hermes_fake.py").write_text(_HERMES_FAKE_PY, encoding="utf-8")
    _fake_bin(binpath, "hermes", 'exec python3 "$(dirname "$0")/_hermes_fake.py" "$@"')
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    return old


def test_hermes_happy_path_with_usage(tmp_path):
    old = _hermes_adapter(tmp_path)
    try:
        result = HermesAdapter(_spec("hm", kind="hermes")).run(
            "write the report", tmp_path, "task-1", timeout=30, log_dir=tmp_path / "logs"
        )
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.exit_code == 0
    assert result.usage == {
        "cost_usd": 0.42, "input_tokens": 100, "output_tokens": 50,
        "cache_read_tokens": 7, "cache_write_tokens": 3, "reasoning_tokens": 9,
        "total_tokens": 150, "model": "test-model",
    }
    log = (tmp_path / "logs" / "hermes-task-1.log").read_text(encoding="utf-8")
    assert "final answer to: write the report" in log


def test_hermes_partial_and_garbage_usage(tmp_path):
    old = _hermes_adapter(tmp_path)
    try:
        os.environ["HERMES_USAGE_MODE"] = "partial"
        partial = HermesAdapter(_spec("hm", kind="hermes")).run(
            "p", tmp_path, "t1", timeout=30, log_dir=tmp_path / "logs"
        )
        del os.environ["HERMES_USAGE_MODE"]

        os.environ["HERMES_USAGE_MODE"] = "garbage"
        garbage = HermesAdapter(_spec("hm", kind="hermes")).run(
            "p", tmp_path, "t2", timeout=30, log_dir=tmp_path / "logs"
        )
        del os.environ["HERMES_USAGE_MODE"]
    finally:
        os.environ["PATH"] = old
    assert partial.ok is True and partial.usage == {"cost_usd": 0.42}
    assert garbage.ok is True and garbage.usage is None  # unreadable report is ignored

    old2 = _hermes_adapter(tmp_path)
    try:
        os.environ["HERMES_USAGE_MODE"] = "tokens_only"
        tokens = HermesAdapter(_spec("hm", kind="hermes")).run(
            "p", tmp_path, "t3", timeout=30, log_dir=tmp_path / "logs2"
        )
        del os.environ["HERMES_USAGE_MODE"]

        os.environ["HERMES_USAGE_MODE"] = "nowrite"
        nowrite = HermesAdapter(_spec("hm", kind="hermes")).run(
            "p", tmp_path, "t4", timeout=30, log_dir=tmp_path / "logs2"
        )
        del os.environ["HERMES_USAGE_MODE"]

        os.environ["HERMES_USAGE_MODE"] = "empty"
        empty = HermesAdapter(_spec("hm", kind="hermes")).run(
            "p", tmp_path, "t5", timeout=30, log_dir=tmp_path / "logs2"
        )
        del os.environ["HERMES_USAGE_MODE"]
    finally:
        os.environ["PATH"] = old2
    assert tokens.ok is True and tokens.usage == {"input_tokens": 5}  # no cost field in report
    assert nowrite.ok is True and nowrite.usage is None  # agent never wrote a usage file
    assert empty.ok is True and empty.usage is None  # report maps to nothing


def test_hermes_build_command(tmp_path):
    adapter = HermesAdapter(_spec("hm", kind="hermes", model="anthropic/claude-sonnet-4", effort="high"))
    cmd = adapter.build_command("the prompt", tmp_path, "task-9", {"maestro_log_dir": "/tmp/logs"})
    assert cmd[0] == "hermes" and cmd[1] == "-z" and cmd[2] == "the prompt"
    assert ["--usage-file", "/tmp/logs/usage-task-9.json"] in [cmd[i:i + 2] for i in range(len(cmd) - 1)]
    assert ["-m", "anthropic/claude-sonnet-4"] in [cmd[i:i + 2] for i in range(len(cmd) - 1)]
    assert ["--reasoning", "high"] in [cmd[i:i + 2] for i in range(len(cmd) - 1)]
    assert cmd[-1] == "--yolo"
    bare = HermesAdapter(_spec("hm", kind="hermes")).build_command("p", tmp_path, "t", {})
    assert "--usage-file" not in bare and "-m" not in bare and "--reasoning" not in bare


def test_hermes_no_usage_file(tmp_path):
    old = _hermes_adapter(tmp_path)
    try:
        result = HermesAdapter(_spec("hm", kind="hermes")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.usage is None  # no log_dir -> no --usage-file


def test_pi_start_command_not_implemented(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "pi", "exit 0")
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"

    class _Rpc(CodexAdapter):
        mode = "rpc"

        def build_command(self, prompt, workspace, task_id, settings):
            return ["pi"]

    try:
        with pytest.raises(NotImplementedError, match="does not speak an rpc protocol"):
            _Rpc(_spec("x")).run("p", tmp_path, "t")
    finally:
        os.environ["PATH"] = old


def test_pi_event_not_implemented(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(binpath, "pi", 'echo \'{"type": "x"}\'\nexit 0')
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"

    class _PartialRpc(CodexAdapter):
        mode = "rpc"

        def build_command(self, prompt, workspace, task_id, settings):
            return ["pi"]

        def rpc_start_command(self, prompt, task_id):
            return {}

    try:
        with pytest.raises(NotImplementedError, match="does not speak an rpc protocol"):
            _PartialRpc(_spec("x")).run("p", tmp_path, "t")
    finally:
        os.environ["PATH"] = old


def test_pi_cancel_without_abort_support(tmp_path):
    class _NoAbort(PiAdapter):
        def rpc_abort_command(self):
            return BaseAdapter.rpc_abort_command(self)

    old = _pi_adapter(tmp_path, behavior="settle")
    try:
        result = _NoAbort(_spec("pi", kind="pi")).run(
            "p", tmp_path, "t", timeout=30, should_cancel=lambda: True
        )
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "was canceled" in (result.error or "")


def test_pi_streaming_oserror(monkeypatch, tmp_path):
    import subprocess

    import maestro.adapters.base as base_mod

    old = _pi_adapter(tmp_path, behavior="settle")
    real_popen = base_mod.subprocess.Popen

    class _Popen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._once = True

        def wait(self, timeout=None):
            if self._once:  # first wait (post-settle grace window) breaks
                self._once = False
                raise OSError("stream broke")
            return real_popen.wait(self, timeout)

    monkeypatch.setattr(base_mod.subprocess, "Popen", _Popen)
    try:
        result = PiAdapter(_spec("pi", kind="pi")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
    assert result.ok is False and "failed while streaming output" in (result.error or "")


# ---------------------------------------------------------------- cursor
_CURSOR_FAKE = r"""
if [ "$1" = "status" ]; then
    if [ "${CURSOR_AUTH:-ok}" = "ok" ]; then echo "logged in as tester"; exit 0; fi
    echo "not logged in" >&2
    exit 1
fi
cat > /dev/null
echo ""
echo 'garbage non-json line'
echo '{"type":"system","subtype":"init","model":"gpt-5"}'
if [ "${CURSOR_USAGE:-yes}" = "yes" ]; then
    echo '{"type":"result","subtype":"success","is_error":false,"duration_ms":10,"result":"done: file created","session_id":"s1","usage":{"inputTokens":100,"outputTokens":25,"cacheReadTokens":7,"cacheWriteTokens":2}}'
elif [ "${CURSOR_USAGE}" = "empty" ]; then
    echo '{"type":"result","subtype":"success","is_error":false,"duration_ms":10,"result":"done: file created","session_id":"s1","usage":{}}'
else
    echo '{"type":"result","subtype":"success","is_error":false,"duration_ms":10,"result":"done: file created","session_id":"s1"}'
fi
exit ${CURSOR_RC:-0}
"""


def _cursor_adapter(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir(exist_ok=True)
    _fake_bin(binpath, "cursor-agent", _CURSOR_FAKE)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    return old


def test_cursor_happy_path(tmp_path):
    old = _cursor_adapter(tmp_path)
    try:
        result = CursorAdapter(_spec("cu", kind="cursor")).run(
            "make a file", tmp_path, "task-1", timeout=30, log_dir=tmp_path / "logs"
        )

        # Result without the usage object parses to no usage (older builds).
        os.environ["CURSOR_USAGE"] = "no"
        plain = CursorAdapter(_spec("cu", kind="cursor")).run("p", tmp_path, "t2", timeout=30)
        del os.environ["CURSOR_USAGE"]

        # Present-but-empty usage object also parses to no usage.
        os.environ["CURSOR_USAGE"] = "empty"
        empty = CursorAdapter(_spec("cu", kind="cursor")).run("p", tmp_path, "t3", timeout=30)
        del os.environ["CURSOR_USAGE"]
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.exit_code == 0
    assert result.usage == {
        "input_tokens": 100, "output_tokens": 25,
        "cache_read_tokens": 7, "cache_write_tokens": 2,
    }
    log = (tmp_path / "logs" / "cursor-task-1.log").read_text(encoding="utf-8")
    assert '"type": "result"' in log or '"type":"result"' in log
    assert "done: file created" in log
    assert plain.ok is True and plain.usage is None
    assert empty.ok is True and empty.usage is None


def test_cursor_failure(tmp_path):
    old = _cursor_adapter(tmp_path)
    os.environ["CURSOR_RC"] = "1"
    try:
        result = CursorAdapter(_spec("cu", kind="cursor")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
        del os.environ["CURSOR_RC"]
    assert result.ok is False and "exited with code 1" in (result.error or "")


def test_cursor_build_command_and_auth_probe(tmp_path):
    adapter = CursorAdapter(_spec("cu", kind="cursor", model="gpt-5"))
    cmd = adapter.build_command("p", tmp_path, "t", {})
    assert cmd == ["cursor-agent", "-p", "--output-format", "json", "--yolo", "--trust", "--model", "gpt-5"]
    assert CursorAdapter(_spec("cu", kind="cursor")).auth_probe() == ["cursor-agent", "status"]

    old = _cursor_adapter(tmp_path)
    try:
        ok = CursorAdapter(_spec("cu", kind="cursor")).preflight()
        assert ok.ok is True and ok.version  # binary + version + status probe all pass
        os.environ["CURSOR_AUTH"] = "bad"
        bad = CursorAdapter(_spec("cu", kind="cursor")).preflight()
        del os.environ["CURSOR_AUTH"]
        assert bad.ok is False and "not authenticated" in (bad.error or "")
    finally:
        os.environ["PATH"] = old


# ---------------------------------------------------------------- openhands
_OPENHANDS_FAKE = r"""
file=""
prev=""
for a in "$@"; do
    if [ "$prev" = "-f" ]; then file="$a"; fi
    prev="$a"
done
if [ -n "$file" ] && [ -f "$file" ]; then cat "$file" > /dev/null; fi
echo '{"type":"action","action":"write","path":"app.py"}'
echo '{"type":"observation","content":"File created successfully"}'
exit ${OPENHANDS_RC:-0}
"""


def _openhands_adapter(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir(exist_ok=True)
    _fake_bin(binpath, "openhands", _OPENHANDS_FAKE)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    return old


def test_openhands_happy_path_with_task_file(tmp_path):
    old = _openhands_adapter(tmp_path)
    try:
        result = OpenHandsAdapter(_spec("oh", kind="openhands")).run(
            "write a flask app", tmp_path, "task-1", timeout=30, log_dir=tmp_path / "logs"
        )
    finally:
        os.environ["PATH"] = old
    assert result.ok is True and result.exit_code == 0
    task_file = tmp_path / "logs" / "prompt-task-1.txt"
    assert task_file.read_text(encoding="utf-8") == "write a flask app"  # -f path carried the prompt
    log = (tmp_path / "logs" / "openhands-task-1.log").read_text(encoding="utf-8")
    assert '"type":"action"' in log and "File created successfully" in log


def test_openhands_failure(tmp_path):
    old = _openhands_adapter(tmp_path)
    os.environ["OPENHANDS_RC"] = "1"
    try:
        result = OpenHandsAdapter(_spec("oh", kind="openhands")).run("p", tmp_path, "t", timeout=30)
    finally:
        os.environ["PATH"] = old
        del os.environ["OPENHANDS_RC"]
    assert result.ok is False and "exited with code 1" in (result.error or "")


def test_openhands_build_command_argv_fallback(tmp_path):
    adapter = OpenHandsAdapter(_spec("oh", kind="openhands"))
    cmd = adapter.build_command("the prompt", tmp_path, "t9", {})
    assert cmd == ["openhands", "--headless", "--json", "--exit-without-confirmation", "-t", "the prompt"]


# ---------------------------------------------------------------- generic onboarding (M5 recipes)
def test_generic_onboarding_openclaw_recipe(tmp_path):
    # The OpenClaw recipe from docs/agent-onboarding.md, run through the real
    # generic path with a fake CLI speaking its documented contract.
    binpath = tmp_path / "bin"
    binpath.mkdir()
    _fake_bin(
        binpath,
        "openclaw",
        'cat > /dev/null\necho \'{"type":"result","cost_usd":0.19}\'\nexit 0',
    )
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        spec = AgentSpec(
            name="openclaw", kind="generic", display_name="OpenClaw",
            command="openclaw agent exec --json --message-file -",
            input_mode="stdin", output_format="jsonl", workspace_policy="cwd",
        )
        from maestro.agents import validate_agent_spec

        validate_agent_spec(spec)
        result = make_adapter(spec).run("fix the failing test", tmp_path, "task-1", timeout=30)
    finally:
        os.environ["PATH"] = old
    assert result.ok is True
    assert (result.usage or {}).get("cost_usd") == 0.19  # jsonl cost hint picked up


def test_probe_codex_no_cli_visible(tmp_path, monkeypatch):
    from maestro.adapters.codex import probe_codex_autonomy_flags

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert probe_codex_autonomy_flags() == ["--approve-for-me"]


def test_probe_codex_probe_failure_falls_back(tmp_path, monkeypatch):
    import maestro.adapters.codex as cx

    def boom(*a, **k):
        raise OSError("probe exploded")

    monkeypatch.setattr(cx.subprocess, "run", boom)
    assert cx.probe_codex_autonomy_flags("/bin/true") == ["--approve-for-me"]
