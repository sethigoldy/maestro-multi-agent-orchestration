"""v2-M5: CopilotAdapter — spawn mode on the GitHub Copilot CLI (fake binary)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from maestro.adapters import CopilotAdapter, make_adapter
from maestro.agents import AgentSpec


def _fake_bin(dirpath: Path, name: str, body: str) -> Path:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


#: POSIX-sh helper: find the value following --usage-output-file in argv.
_ARGV_SCAN = '''prev=""
for a in "$@"; do
  if [ "$prev" = "--usage-output-file" ]; then UF="$a"; fi
  prev="$a"
done
'''

USAGE_FILE_BODY = json.dumps(
    {
        "totalPremiumRequestCost": 3,
        "totalUserRequests": 2,
        "totalNanoAiu": 7152150000,
        "tokenDetails": {
            "input": {"tokenCount": 12},
            "cache_read": {"tokenCount": 4},
            "cache_write": {"tokenCount": 99},
            "output": {"tokenCount": 7},
        },
    }
)


def _spec(**kw) -> AgentSpec:
    return AgentSpec(name="cp", kind="copilot", **kw)


def _run(adapter, tmp_path, prompt="do the thing", task_id="task-cp", **kw):
    lines: list[str] = []
    result = adapter.run(prompt, Path(tmp_path), task_id, on_line=lines.append, **kw)
    return result, lines


def test_build_command_shape_and_model():
    adapter = CopilotAdapter(_spec())
    command = adapter.build_command("hello world", Path("/ws"), "t1", {})
    assert command[:2] == ["copilot", "-p"]
    assert "hello world" in command
    assert "--output-format" in command and "json" in command
    assert "--yolo" in command and "-C" in command and "/ws" in command
    assert "--usage-output-file" in command
    usage_arg = command[command.index("--usage-output-file") + 1]
    assert usage_arg.endswith("maestro-copilot-usage-t1.json")
    assert "--model" not in command

    adapter_m = CopilotAdapter(_spec(model="gpt-5.4"))
    command_m = adapter_m.build_command("p", Path("/ws"), "t1", {})
    assert command_m[-2:] == ["--model", "gpt-5.4"]
    # settings model is the fallback when the spec has none
    adapter_s = CopilotAdapter(_spec())
    command_s = adapter_s.build_command("p", Path("/ws"), "t1", {"model": "claude-sonnet-5"})
    assert command_s[-2:] == ["--model", "claude-sonnet-5"]


def test_input_mode_is_arg():
    assert CopilotAdapter(_spec()).input_mode() == "arg"


def test_run_end_to_end_with_usage_file(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    body = (
        "cat > /dev/null\n"
        + _ARGV_SCAN
        + 'echo \'{"type": "session.start", "data": {}}\'\n'
        + "echo 'not json at all'\n"
        + f'echo \'{USAGE_FILE_BODY}\' > "$UF"\n'
        + 'echo \'{"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1, "totalApiDurationMs": 1549}}\'\n'
        + "echo final-line\n"
    )
    _fake_bin(binpath, "copilot", body)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        adapter = CopilotAdapter(_spec())
        result, lines = _run(adapter, tmp_path)
    finally:
        os.environ["PATH"] = old

    assert result.ok is True and result.exit_code == 0
    # live output lines are surfaced (non-JSON included); usage merged from the file
    assert "final-line" in lines
    assert result.usage is not None
    assert result.usage["input_tokens"] == 12
    assert result.usage["output_tokens"] == 7
    assert result.usage["cache_read_tokens"] == 4
    assert result.usage["cache_write_tokens"] == 99
    assert result.usage["totalPremiumRequestCost"] == 3
    # the result-line counters are merged too (file wins on collisions)
    assert result.usage["premiumRequests"] == 1
    # usage file is consumed, not left behind
    assert not (Path(tempfile.gettempdir()) / "maestro-copilot-usage-task-cp.json").exists()


def test_run_failed_exit_still_reads_usage(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    body = (
        "cat > /dev/null\n"
        + _ARGV_SCAN
        + f'echo \'{USAGE_FILE_BODY}\' > "$UF"\n'
        + "echo 'boom'\n"
        + "exit 3\n"
    )
    _fake_bin(binpath, "copilot", body)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        adapter = CopilotAdapter(_spec())
        result, _ = _run(adapter, tmp_path, task_id="task-fail")
    finally:
        os.environ["PATH"] = old

    assert result.ok is False and result.exit_code == 3
    assert result.usage is not None and result.usage["output_tokens"] == 7


def test_run_no_usage_file_leaves_result_usage_alone(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    body = """cat > /dev/null
echo '{"type": "result", "exitCode": 0, "usage": {"premiumRequests": 2}}'
"""
    _fake_bin(binpath, "copilot", body)
    old = os.environ.get("PATH", "")
    (Path(tempfile.gettempdir()) / "maestro-copilot-usage-task-none.json").unlink(missing_ok=True)
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        adapter = CopilotAdapter(_spec())
        result, _ = _run(adapter, tmp_path, task_id="task-none")
    finally:
        os.environ["PATH"] = old

    assert result.ok is True
    # only the result-line counters survive (no file to merge)
    assert result.usage == {"premiumRequests": 2}


def test_run_malformed_usage_file_ignored(tmp_path):
    binpath = tmp_path / "bin"
    binpath.mkdir()
    body = (
        "cat > /dev/null\n"
        + _ARGV_SCAN
        + 'echo \'this is not json\' > "$UF"\n'
        + 'echo \'{"type": "result", "exitCode": 0}\'\n'
    )
    _fake_bin(binpath, "copilot", body)
    old = os.environ.get("PATH", "")
    (Path(tempfile.gettempdir()) / "maestro-copilot-usage-task-bad.json").unlink(missing_ok=True)
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"
    try:
        adapter = CopilotAdapter(_spec())
        result, _ = _run(adapter, tmp_path, task_id="task-bad")
    finally:
        os.environ["PATH"] = old

    assert result.ok is True and result.usage is None  # no usable usage anywhere


def test_parse_line_shapes():
    adapter = CopilotAdapter(_spec())
    assert adapter.parse_line("") is None
    assert adapter.parse_line("not json") is None
    assert adapter.parse_line(json.dumps({"type": "session.start"})) is None
    assert adapter.parse_line(json.dumps({"type": "result"})) is None  # no usage object
    out = adapter.parse_line(json.dumps({"type": "result", "usage": {"premiumRequests": 5}}))
    assert out == {"usage": {"premiumRequests": 5}}
    out = adapter.parse_line(json.dumps({"type": "result", "usage": {"weird": "x"}}))
    assert out is None  # no numeric counters -> nothing to report


def test_map_usage_file_model_metrics_fallback():
    from maestro.adapters.copilot import _map_usage_file

    data = {
        "modelMetrics": {
            "claude-sonnet-5": {
                "usage": {"inputTokens": 100, "outputTokens": 5, "cacheReadTokens": 1, "cacheWriteTokens": 2, "reasoningTokens": 3},
            }
        },
        "totalPremiumRequestCost": 9,
    }
    mapped = _map_usage_file(data)
    assert mapped["input_tokens"] == 100 and mapped["output_tokens"] == 5
    assert mapped["reasoning_tokens"] == 3 and mapped["totalPremiumRequestCost"] == 9

    # tokenDetails wins over modelMetrics; non-dict buckets are skipped
    data2 = {
        "tokenDetails": {"input": {"tokenCount": 7}, "output": "garbage"},
        "modelMetrics": {"m": {"usage": {"inputTokens": 100}}},
    }
    mapped2 = _map_usage_file(data2)
    assert mapped2["input_tokens"] == 7 and "output_tokens" not in mapped2

    # non-dict model entries and missing usage are skipped
    assert _map_usage_file({"modelMetrics": {"m": "nope"}, "totalUserRequests": 4}) == {"totalUserRequests": 4}
    assert _map_usage_file({"modelMetrics": {"m": {}}}) == {}  # dict entry without a usage object


def test_make_adapter_dispatches_copilot():
    assert isinstance(make_adapter(_spec()), CopilotAdapter)


def test_run_usage_file_unlink_failure(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    binpath = tmp_path / "bin"
    binpath.mkdir()
    body = (
        "cat > /dev/null\n"
        + _ARGV_SCAN
        + 'echo \'{"tokenDetails": {"output": {"tokenCount": 9}}}\' > "$UF"\n'
        + 'echo \'{"type": "result", "exitCode": 0}\'\n'
    )
    _fake_bin(binpath, "copilot", body)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{binpath}{os.pathsep}{old}"

    real_unlink = _Path.unlink

    def broken(self, *a, **k):
        raise OSError("sticky filesystem")

    monkeypatch.setattr(_Path, "unlink", broken)
    try:
        adapter = CopilotAdapter(_spec())
        result, _ = _run(adapter, tmp_path, task_id="task-sticky")
    finally:
        os.environ["PATH"] = old
        # clean up the file the real unlink would have removed (patch still active here)
        try:
            real_unlink(Path(tempfile.gettempdir()) / "maestro-copilot-usage-task-sticky.json")
        except OSError:
            pass

    assert result.ok is True and result.usage == {"output_tokens": 9}  # merge survived the unlink failure


def test_preflight_missing_binary(tmp_path, monkeypatch):
    adapter = CopilotAdapter(_spec())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # nothing on PATH
    preflight = adapter.preflight()
    assert not preflight.ok and "copilot" in (preflight.error or "")
