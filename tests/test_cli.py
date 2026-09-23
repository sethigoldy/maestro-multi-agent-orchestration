from pathlib import Path
import json
import stat
import sys

from maestro import cli

def test_version(): assert cli.VERSION=='0.12.0'

def test_normalize_task_shortcut(): assert cli._normalize_argv(['task','abc'])==['task','status','abc']

def test_empty_workspace_uses_home(monkeypatch):
    monkeypatch.setenv('MAESTRO_WORKSPACE','')
    assert cli._workspace(None)==Path.home().resolve()


def test_cli_env_and_raw_argv_paths(monkeypatch):
    monkeypatch.setenv("MAESTRO_WORKSPACE", "   ")
    assert cli._workspace(None) == Path.home().resolve()
    assert cli._workspace("") == Path.home().resolve()
    # argv never includes the program name, so "task" in the second position
    # is an argument of another command and is not rewritten.
    assert cli._normalize_argv(["prog", "task", "abc"]) == ["prog", "task", "abc"]


def test_cli_scope_empty_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MAESTRO_WORKSPACE", "")
    class FakeMaestro:
        @staticmethod
        def _resolve_project_root(path): return path
    monkeypatch.setattr(cli, "Maestro", FakeMaestro)
    args = type("A", (), {"project": None, "workspace": None})()
    base, scope = cli._scope_for_list(args)
    assert base == Path.home().resolve() and scope is None

def test_task_lookup_is_global(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("MAESTRO_HOME", str(home))

    # Populate the global registry/task store first.
    # Then invoke lookup from a different workspace and assert
    # the task is still resolvable.

def test_normalize_argv_after_top_level_option():
    assert cli._normalize_argv(
        ["--workspace", "/repo", "task", "task-123"]
    ) == [
        "--workspace",
        "/repo",
        "task",
        "status",
        "task-123",
    ]

def test_normalize_argv_named_task_commands_unchanged():
    assert cli._normalize_argv(
        ["--workspace", "/repo", "task", "status", "task-123"]
    ) == ["--workspace", "/repo", "task", "status", "task-123"]

    assert cli._normalize_argv(
        ["task", "list"]
    ) == ["task", "list"]

def test_task_workspace_empty_explicit_uses_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert cli._task_workspace("") == tmp_path.resolve()


def test_task_workspace_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MAESTRO_WORKSPACE", str(tmp_path))
    assert cli._task_workspace(None) == tmp_path.resolve()


def test_task_workspace_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("MAESTRO_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli._task_workspace(None) == tmp_path.resolve()


def _fake_executable(path: Path, body: str = "echo 'codex 9.9.9'") -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_agents_cli_lifecycle(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("PATH", str(bindir))

    def run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["maestro", *argv])
        return cli.main()

    assert run("agents", "add", "--name", "codex", "--kind", "codex",
               "--display-name", "Codex CLI", "--skill", "implementation") == 0
    added = json.loads(capsys.readouterr().out)
    assert added["name"] == "codex" and added["kind"] == "codex"
    assert added["skills"] == ["implementation"] and added["display_name"] == "Codex CLI"

    assert run("agents", "list") == 0
    items = json.loads(capsys.readouterr().out)
    assert [i["name"] for i in items] == ["codex"]

    assert run("agents", "discover") == 0
    found = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert found["codex"]["found"] is True and found["claude_code"]["found"] is False

    assert run("agents", "status", "codex") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["registered"] is True and status["found"] is True
    assert status["version"] == "codex 9.9.9"

    assert run("agents", "status", "ghost") == 0
    assert json.loads(capsys.readouterr().out) == {"name": "ghost", "registered": False}

    assert run("agents", "add", "--name", "remote-b", "--kind", "a2a_remote",
               "--command", "http://10.0.0.5:8790", "--token", "sekrit") == 0
    remote = json.loads(capsys.readouterr().out)
    assert remote["kind"] == "a2a_remote" and remote["command"] == "http://10.0.0.5:8790"
    assert remote["token"] == "<redacted>"  # printed output never carries the token
    stored = (home / "agents" / "remote-b.toml")
    assert 'token = "sekrit"' in stored.read_text(encoding="utf-8")  # the registry keeps the real one
    assert stored.stat().st_mode & 0o777 == 0o600
    assert run("agents", "remove", "remote-b") == 0
    capsys.readouterr()

    assert run("agents", "add", "--name", "mycli", "--kind", "generic",
               "--command", "mycli --run {prompt}", "--input-mode", "stdin",
               "--output-format", "jsonl", "--workspace-policy", "flag") == 0
    generic = json.loads(capsys.readouterr().out)
    assert generic["kind"] == "generic" and generic["input_mode"] == "stdin"
    assert generic["output_format"] == "jsonl" and generic["workspace_policy"] == "flag"

    assert run("agents", "remove", "mycli") == 0
    assert json.loads(capsys.readouterr().out) == {"name": "mycli", "removed": True}

    assert run("agents", "remove", "ghost") == 2
    assert "not registered" in capsys.readouterr().err


def _delegate_capture(monkeypatch):
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9")
    captured = {}

    def fake_post(url, method, payload, token=None):
        captured["payload"] = payload
        return {"task": {"id": "task-x"}}

    monkeypatch.setattr(cli, "_post_jsonrpc", fake_post)
    monkeypatch.setattr(cli, "_stream_task", lambda url, task_id, token=None: 0)
    return captured


def test_delegate_mode_flag_in_payload(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _delegate_capture(monkeypatch)
    rc = cli.main(["delegate", "--title", "T", "--request", "R", "--mode", "economy"])
    assert rc == 0
    data = captured["payload"]["message"]["parts"][0]["data"]
    assert data["routing"]["mode"] == "economy"
    assert "mode=economy" in capsys.readouterr().out


def test_delegate_mode_flag_with_target(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _delegate_capture(monkeypatch)
    rc = cli.main(["delegate", "--title", "T", "--request", "R", "--target", "mine", "--mode", "economy"])
    assert rc == 0
    data = captured["payload"]["message"]["parts"][0]["data"]
    assert data["routing"]["mode"] == "economy" and data["routing"]["target_agent"] == "mine"


def test_delegate_mode_overrides_file_mode(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    handoff = tmp_path / "h.toml"
    handoff.write_text('[handoff]\ntitle = "T"\nrequest = "R"\n[routing]\nmode = "other"\n', encoding="utf-8")
    captured = _delegate_capture(monkeypatch)
    rc = cli.main(["delegate", "--file", str(handoff), "--mode", "economy"])
    assert rc == 0
    data = captured["payload"]["message"]["parts"][0]["data"]
    assert data["routing"]["mode"] == "economy"


def test_delegate_mode_requires_title_and_request(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _delegate_capture(monkeypatch)
    rc = cli.main(["delegate", "--mode", "economy"])
    assert rc == 2


def _followup_capture(monkeypatch, result=None, stream_rc=0):
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9")
    captured = {}

    def fake_post(url, method, payload, token=None):
        captured["method"] = method
        captured["payload"] = payload
        return result if result is not None else {"task": {"id": "task-c"}}

    def fake_stream(url, task_id, token=None):
        captured["streamed"] = task_id
        return stream_rc

    monkeypatch.setattr(cli, "_post_jsonrpc", fake_post)
    monkeypatch.setattr(cli, "_stream_task", fake_stream)
    return captured


def test_task_continue_posts_followup_and_streams(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _followup_capture(monkeypatch)
    rc = cli.main(["task", "continue", "task-c", "--request", "keep going"])
    assert rc == 0
    assert captured["method"] == "tasks/followup"
    assert captured["payload"] == {"id": "task-c", "instruction": "keep going", "context_mode": "reuse"}
    assert captured.get("streamed") == "task-c"
    out = capsys.readouterr().out
    assert "[task] task-c — continuation (context=reuse) resuming" in out


def test_task_continue_fresh_context_flag(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _followup_capture(monkeypatch)
    rc = cli.main(["task", "continue", "task-c", "--request", "clean turn", "--context", "fresh"])
    assert rc == 0
    assert captured["payload"]["context_mode"] == "fresh"
    assert "context=fresh" in capsys.readouterr().out


def test_task_continue_no_wait_prints_json(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _followup_capture(monkeypatch)
    rc = cli.main(["task", "continue", "task-c", "--request", "x", "--no-wait"])
    assert rc == 0
    assert "streamed" not in captured
    assert json.loads(capsys.readouterr().out)["task"]["id"] == "task-c"


def test_task_continue_error_result_prints_json(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _followup_capture(monkeypatch, result={"error": {"code": -32004, "message": "Unknown task"}})
    rc = cli.main(["task", "continue", "task-gone", "--request", "x"])
    assert rc == 0
    assert "streamed" not in captured
    assert json.loads(capsys.readouterr().out)["error"]["code"] == -32004


def test_config_lists_modes(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    (home / "config.toml").write_text(
        '[modes.economy]\nimplementer = "impl"\nreviewer = "rev"\nmax_bounces = 1\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["modes"]["economy"] == {"implementer": "impl", "verifier": None, "reviewer": "rev", "fixer": None, "max_bounces": 1}


def test_config_without_modes_key(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["modes"] == {}


def test_config_lists_continuation(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    (home / "config.toml").write_text(
        "[continuation]\nenabled = false\nmax_tokens = 900\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["continuation"] == {"enabled": False, "max_tokens": 900}


def test_config_continuation_defaults(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["continuation"] == {"enabled": True, "max_tokens": 6000}


def test_delegate_context_flags_in_payload(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    captured = _delegate_capture(monkeypatch)
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("s", encoding="utf-8")
    rc = cli.main([
        "delegate", "--title", "T", "--request", "R", "--target", "mine",
        "--context", "Be terse.", "--context", "Second note.",
        "--context-file", str(spec_file),
        "--skill", "/home/u/skills/pdf",
    ])
    assert rc == 0
    data = captured["payload"]["message"]["parts"][0]["data"]
    assert data["context"] == [
        {"label": "context-1", "kind": "text", "text": "Be terse."},
        {"label": "context-2", "kind": "text", "text": "Second note."},
        {"label": "spec", "kind": "file", "path": str(spec_file)},
        {"label": "pdf", "kind": "skill", "path": "/home/u/skills/pdf"},
    ]


def test_config_lists_context(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    (home / "config.toml").write_text(
        '[context.style]\ntext = "Be terse."\n\n[context.pdf-skill]\nkind = "skill"\npath = "~/skills/pdf"\nphases = ["implementer"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["context"]["style"] == {"label": "style", "kind": "text", "text": "Be terse.", "source": "user config"}
    assert out["context"]["pdf-skill"]["kind"] == "skill" and out["context"]["pdf-skill"]["phases"] == ["implementer"]
