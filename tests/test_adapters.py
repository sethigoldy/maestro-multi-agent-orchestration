from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from maestro.adapters import AdapterNotAvailable, ClaudeCodeAdapter, CodexAdapter, GenericAdapter, make_adapter
from maestro.agents import AgentSpec


def _fake_bin(dirpath: Path, name: str, body: str) -> Path:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _spec(name: str, kind: str = "codex", **kw) -> AgentSpec:
    return AgentSpec(name=name, kind=kind, **kw)


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
def test_codex_command_with_settings(tmp_path):
    adapter = CodexAdapter(_spec("codex", model="gpt-x", effort="max"))
    cmd = adapter.build_command("prompt", tmp_path, "task-1", {})
    assert cmd[:3] == ["codex", "exec", "--full-auto"]
    assert ["--model", "gpt-x"] in [cmd[i : i + 2] for i in range(len(cmd) - 1)]
    assert any('model_reasoning_effort="max"' in part for part in cmd)
    assert cmd[-1] == "-"


def test_codex_command_task_settings_override_spec(tmp_path):
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
    with pytest.raises(AdapterNotAvailable):
        make_adapter(_spec("pi", kind="pi"))


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
    _fake_bin(binpath, "codex", 'cat > /dev/null\nsleep 30')
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
    _fake_bin(binpath, "codex", 'cat > /dev/null\ni=0\nwhile [ $i -lt 100 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
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
    class _Rpc(CodexAdapter):
        mode = "rpc"

    with pytest.raises(AdapterNotAvailable):
        _Rpc(_spec("x")).run("p", tmp_path, "t")


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
    _fake_bin(binpath, "codex", 'cat > /dev/null\ntrap "" TERM\necho alive\nsleep 30')
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
    _fake_bin(binpath, "codex", 'cat > /dev/null\ntrap "" TERM\necho alive\nsleep 30')
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
