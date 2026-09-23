"""Regression tests for library correctness fixes.

Each test in this file reproduces one confirmed bug. They are grouped by the
module that holds the fix, and each group names the bug it covers.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maestro.adapters import ClineAdapter, CopilotAdapter, CursorAdapter, GenericAdapter, HermesAdapter, PiAdapter
from maestro.agents import AgentRegistry, AgentSpec, _dump_toml, _toml_scalar


# ---------------------------------------------------------------------------
# Bug 1: a per-task model or effort override must beat the registry default.
# The daemon merges the handoff override into ``settings``, so the adapters
# must read ``settings`` first and fall back to the registry spec.
# ---------------------------------------------------------------------------

_OVERRIDE_ADAPTERS = [
    (CopilotAdapter, "copilot", "--model"),
    (CursorAdapter, "cursor", "--model"),
    (HermesAdapter, "hermes", "-m"),
    (ClineAdapter, "cline", "--model"),
    (PiAdapter, "pi", "--model"),
]


@pytest.mark.parametrize("adapter_cls,kind,flag", _OVERRIDE_ADAPTERS)
def test_task_model_override_beats_registry_model(tmp_path, adapter_cls, kind, flag):
    adapter = adapter_cls(AgentSpec(name="a", kind=kind, model="registry-model"))
    cmd = adapter.build_command("do it", tmp_path, "t1", {"model": "task-model"})
    assert cmd[cmd.index(flag) + 1] == "task-model"
    assert "registry-model" not in cmd


@pytest.mark.parametrize("adapter_cls,kind,flag", _OVERRIDE_ADAPTERS)
def test_registry_model_used_when_task_sets_none(tmp_path, adapter_cls, kind, flag):
    adapter = adapter_cls(AgentSpec(name="a", kind=kind, model="registry-model"))
    cmd = adapter.build_command("do it", tmp_path, "t1", {})
    assert cmd[cmd.index(flag) + 1] == "registry-model"


@pytest.mark.parametrize("adapter_cls,kind,flag", [(HermesAdapter, "hermes", "--reasoning"), (ClineAdapter, "cline", "--thinking")])
def test_task_effort_override_beats_registry_effort(tmp_path, adapter_cls, kind, flag):
    adapter = adapter_cls(AgentSpec(name="a", kind=kind, effort="low"))
    cmd = adapter.build_command("do it", tmp_path, "t1", {"effort": "high"})
    assert cmd[cmd.index(flag) + 1] == "high"
    assert "low" not in cmd
    fallback = adapter.build_command("do it", tmp_path, "t1", {})
    assert fallback[fallback.index(flag) + 1] == "low"


# ---------------------------------------------------------------------------
# Bug 2: the generic command template must survive real paths and prompts.
# ---------------------------------------------------------------------------

def _generic(command: str, input_mode: str = "arg") -> GenericAdapter:
    return GenericAdapter(AgentSpec(name="g", kind="generic", command=command, input_mode=input_mode))


def test_generic_stdin_mode_keeps_workspace_with_spaces_as_one_argument():
    adapter = _generic("mytool --dir {workspace} --id {task_id}", input_mode="stdin")
    cmd = adapter.build_command("ignored", Path("/Users/me/My Projects/app"), "t1", {})
    assert cmd == ["mytool", "--dir", "/Users/me/My Projects/app", "--id", "t1"]


def test_generic_stdin_mode_accepts_workspace_with_apostrophe():
    adapter = _generic("mytool --dir {workspace}", input_mode="stdin")
    cmd = adapter.build_command("ignored", Path("/Users/me/it's here"), "t1", {})
    assert cmd == ["mytool", "--dir", "/Users/me/it's here"]


def test_generic_stdin_mode_leaves_prompt_placeholder_alone():
    adapter = _generic("mytool {prompt}", input_mode="stdin")
    assert adapter.build_command("the prompt", Path("/w"), "t1", {}) == ["mytool", "{prompt}"]


def test_generic_arg_mode_does_not_substitute_inside_the_prompt():
    adapter = _generic("mytool {prompt} --cwd {workspace} --id {task_id}")
    prompt = "Edit the file in {workspace} for task {task_id}; keep 'quotes' and \"doubles\"."
    cmd = adapter.build_command(prompt, Path("/w s"), "t1", {})
    assert cmd == ["mytool", prompt, "--cwd", "/w s", "--id", "t1"]


def test_generic_arg_mode_substitutes_placeholders_inside_an_argument():
    adapter = _generic("mytool --prompt={prompt} --at='{workspace}/sub'")
    cmd = adapter.build_command("hi there", Path("/a b"), "t1", {})
    assert cmd == ["mytool", "--prompt=hi there", "--at=/a b/sub"]


def test_generic_template_with_broken_quoting_raises_a_clear_error():
    adapter = _generic("mytool 'unclosed {prompt}")
    with pytest.raises(ValueError, match="command template"):
        adapter.build_command("p", Path("/w"), "t1", {})


def test_generic_binary_honours_shell_quoting():
    assert _generic("'/opt/My Tools/agent' --flag {prompt}").binary() == "/opt/My Tools/agent"


def test_generic_binary_with_broken_quoting_is_unknown():
    adapter = _generic("'/opt/My Tools/agent --flag")
    assert adapter.binary() is None
    assert adapter.preflight().ok is False


def test_generic_binary_of_a_blank_template_is_unknown():
    assert _generic("   ").binary() is None


def test_generic_template_run_with_spaced_workspace(tmp_path):
    """End to end: a fake CLI receives the workspace as exactly one argument."""
    bindir = tmp_path / "bin dir"
    bindir.mkdir()
    out = tmp_path / "args.txt"
    tool = bindir / "tool"
    tool.write_text(
        '#!/bin/sh\n[ "$1" = "--go" ] || exit 0\nshift\nfor a in "$@"; do printf "%s\\n" "$a"; done > "' + str(out) + '"\n',
        encoding="utf-8",
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    workspace = tmp_path / "My Projects" / "it's app"
    workspace.mkdir(parents=True)
    adapter = _generic(f"'{tool}' --go --dir {{workspace}}", input_mode="stdin")
    result = adapter.run("the prompt", workspace, "t1", settings={}, timeout=30, log_dir=tmp_path / "logs")
    assert result.ok, result.error
    assert out.read_text(encoding="utf-8").splitlines() == ["--dir", str(workspace)]


# ---------------------------------------------------------------------------
# Bug 3: from_dict must reject wrong types with ValueError.
# ---------------------------------------------------------------------------

def _payload(**sections):
    data = {"handoff": {"title": "t", "request": "r"}, "routing": {}, "expectations": {}, "constraints": {}}
    for name, values in sections.items():
        data.setdefault(name, {})
        if isinstance(values, dict) and isinstance(data[name], dict):
            data[name].update(values)
        else:
            data[name] = values
    return data


@pytest.mark.parametrize(
    "section,key",
    [("routing", "fallback"), ("handoff", "context_files"), ("expectations", "artifacts")],
)
def test_from_dict_rejects_a_plain_string_where_a_list_is_required(section, key):
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match=key):
        from_dict(_payload(**{section: {key: "claude"}}))


def test_from_dict_rejects_non_string_list_items():
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match="fallback"):
        from_dict(_payload(routing={"fallback": ["claude", 3]}))


def test_from_dict_null_max_depth_is_a_value_error():
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match="max_depth_remaining"):
        from_dict(_payload(constraints={"max_depth_remaining": None}))
    with pytest.raises(ValueError, match="max_depth_remaining"):
        from_dict(_payload(constraints={"max_depth_remaining": "many"}))


@pytest.mark.parametrize(
    "key", ["mode", "target_agent", "origin_agent", "review_agent", "verify_agent", "fix_agent", "parent_task_id"]
)
def test_from_dict_rejects_non_string_routing_fields(key):
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match=key):
        from_dict(_payload(routing={key: ["a", "b"]}))


def test_from_dict_null_title_or_request_counts_as_missing():
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match="non-empty title"):
        from_dict(_payload(handoff={"title": None}))
    with pytest.raises(ValueError, match="non-empty request"):
        from_dict(_payload(handoff={"request": None}))


def test_from_dict_rejects_non_string_text_fields():
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match="design"):
        from_dict(_payload(handoff={"design": {"a": 1}}))
    with pytest.raises(ValueError, match="verification"):
        from_dict(_payload(expectations={"verification": ["auto"]}))


def test_from_dict_null_optional_text_fields_use_defaults():
    from maestro.handoff import from_dict

    doc = from_dict(_payload(handoff={"design": None, "context_notes": None}, routing={"target_agent": None, "fallback": None}))
    assert doc.design == "" and doc.context_notes == "" and doc.target_agent == "codex" and doc.fallback == []


def test_from_dict_rejects_wrong_container_types():
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match="context"):
        from_dict(_payload(context="not a list"))
    with pytest.raises(ValueError, match="agent_settings"):
        from_dict(_payload(agent_settings=["model", "x"]))


def test_from_dict_rejects_non_numeric_budget_hint():
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match="budget_hint"):
        from_dict(_payload(expectations={"budget_hint": [1]}))
    with pytest.raises(ValueError, match="budget_hint"):
        from_dict(_payload(expectations={"budget_hint": True}))
    assert from_dict(_payload(expectations={"budget_hint": 2})).budget_hint == 2


# ---------------------------------------------------------------------------
# Bugs 4 and 5: skill context resolution, error handling and staged names.
# ---------------------------------------------------------------------------

def _skill_dir(path: Path, body: str = "skill body") -> Path:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(body, encoding="utf-8")
    return path


def _ctx(label, kind, path=None, text=None):
    from maestro.context import ContextEntry

    return ContextEntry(label=label, kind=kind, text=text, path=path, source="handoff")


def test_relative_skill_path_resolves_against_the_workspace_not_the_cwd(tmp_path, monkeypatch):
    from maestro.context import check_skill_entries, render_context

    cwd = tmp_path / "cwd"
    _skill_dir(cwd / "skills" / "pdf")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(cwd)
    entry = _ctx("pdf", "skill", path="skills/pdf")
    # The delegate-time check and the render step agree: the skill is missing.
    with pytest.raises(ValueError, match="directory not found"):
        check_skill_entries([entry], workspace)
    rendered = render_context([entry], "implementer", workspace, tmp_path / "task", "codex")
    assert 'Skill "pdf" is unavailable' in rendered.block
    assert rendered.skills_root is None
    # Once the skill exists inside the workspace, both steps accept it.
    _skill_dir(workspace / "skills" / "pdf")
    check_skill_entries([entry], workspace)
    rendered = render_context([entry], "implementer", workspace, tmp_path / "task", "codex")
    assert 'Skill "pdf" is available at' in rendered.block


def test_check_skill_entries_requires_skill_md_and_ignores_other_kinds(tmp_path):
    from maestro.context import check_skill_entries

    (tmp_path / "empty").mkdir()
    check_skill_entries([_ctx("note", "text", text="hello")], tmp_path)
    with pytest.raises(ValueError, match="no SKILL.md"):
        check_skill_entries([_ctx("empty", "skill", path="empty")], tmp_path)


def test_skill_copy_failure_degrades_to_a_note(tmp_path, monkeypatch):
    from maestro import context
    from maestro.context import render_context

    skill = _skill_dir(tmp_path / "skill")

    def broken_copytree(src, dst, *args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(context.shutil, "copytree", broken_copytree)
    rendered = render_context([_ctx("s", "skill", path=str(skill))], "implementer", tmp_path, tmp_path / "task", "codex")
    assert 'Skill "s" is unavailable' in rendered.block and "permission denied" in rendered.block
    assert rendered.skills_root is None


def test_labels_with_the_same_slug_stage_separate_skills(tmp_path):
    from maestro.context import render_context

    first = _skill_dir(tmp_path / "one", "first skill")
    second = _skill_dir(tmp_path / "two", "second skill")
    entries = [_ctx("code review", "skill", path=str(first)), _ctx("code/review", "skill", path=str(second))]
    rendered = render_context(entries, "implementer", tmp_path, tmp_path / "task", "codex")
    staged = sorted((rendered.skills_root / ".claude" / "skills").iterdir())
    assert len(staged) == 2
    bodies = sorted((path / "SKILL.md").read_text(encoding="utf-8") for path in staged)
    assert bodies == ["first skill", "second skill"]


def test_labels_with_the_same_slug_keep_separate_large_file_artifacts(tmp_path):
    from maestro.context import FILE_INLINE_LIMIT, render_context

    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("A" * (FILE_INLINE_LIMIT + 1), encoding="utf-8")
    b.write_text("B" * (FILE_INLINE_LIMIT + 1), encoding="utf-8")
    entries = [_ctx("big file", "file", path=str(a)), _ctx("big/file", "file", path=str(b))]
    task_dir = tmp_path / "task"
    render_context(entries, "implementer", tmp_path, task_dir, "codex")
    artifacts = sorted(task_dir.glob("context-*.txt"))
    assert len(artifacts) == 2
    assert sorted(p.read_text(encoding="utf-8")[0] for p in artifacts) == ["A", "B"]


def test_staged_names_avoid_every_collision():
    from maestro.context import _staged_names

    labels = ["x y", "x-y-2", "x/y", "x-y", "X-Y"]
    names = _staged_names([_ctx(label, "text", text="t") for label in labels])
    # The first entry keeps the plain slug; every later clash gets a free suffix,
    # and names that differ only in case also count as a clash.
    assert names == ["x-y", "x-y-2", "x-y-3", "x-y-4", "X-Y-5"]


# ---------------------------------------------------------------------------
# Bug 6: daily spend follows each attempt's own finish time.
# ---------------------------------------------------------------------------

def test_daily_spend_counts_todays_attempt_on_an_older_task():
    from maestro.budgets import check, BudgetCaps, daily_spend

    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    yesterday = (now - timedelta(days=1)).isoformat()
    record = {
        "started_at": yesterday,
        "attempts": [
            {"agent": "codex", "usage": {"cost_usd": 1.0}, "finished_at": yesterday},
            {"agent": "codex", "usage": {"cost_usd": 2.5}, "finished_at": now.isoformat()},
        ],
    }
    assert daily_spend([record], now=now) == pytest.approx(2.5)
    assert check(BudgetCaps(daily_usd=2.0), "codex", [record], now=now) is not None


def test_daily_spend_ignores_yesterdays_attempt_on_a_task_started_today():
    from maestro.budgets import daily_spend

    now = datetime(2026, 9, 23, 0, 30, tzinfo=timezone.utc)
    record = {
        "started_at": now.isoformat(),
        "attempts": [{"agent": "codex", "usage": {"cost_usd": 4.0}, "finished_at": "2026-09-22T23:59:00Z"}],
    }
    assert daily_spend([record], now=now) == 0.0


def test_daily_spend_falls_back_to_task_start_without_finish_time():
    from maestro.budgets import daily_spend

    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    records = [
        {"started_at": now.isoformat(), "attempts": [{"agent": "a", "usage": {"cost_usd": 1.0}}]},
        {"started_at": "2026-09-01T00:00:00+00:00", "attempts": [{"agent": "a", "usage": {"cost_usd": 9.0}, "finished_at": None}]},
        {"started_at": "garbage", "attempts": [{"agent": "a", "usage": {"cost_usd": 5.0}, "finished_at": "also garbage"}]},
        {"started_at": now.isoformat(), "attempts": ["not a dict", {"agent": "a", "usage": {"cost_usd": 0}}]},
    ]
    assert daily_spend(records, now=now) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bug 7: the registry writer must emit valid TOML and never clobber a good file.
# ---------------------------------------------------------------------------

def test_toml_scalar_escapes_every_control_character():
    nasty = "a\rb\x1b[0m\x00\x7f\tc\nd\"e\\f"
    text = f"v = {_toml_scalar(nasty)}\n"
    assert tomllib.loads(text)["v"] == nasty


def test_dump_toml_quotes_keys_that_are_not_bare():
    text = _dump_toml({"plain": 1, "agent_settings": {"my key": "v", "dotted.key": "w"}})
    assert tomllib.loads(text)["agent_settings"] == {"my key": "v", "dotted.key": "w"}


def test_registry_round_trips_control_characters(tmp_path):
    registry = AgentRegistry(tmp_path)
    spec = AgentSpec(name="odd", kind="codex", display_name="Odd\r\x1b[31mRed\x7f")
    registry.save(spec)
    loaded = registry.get("odd")
    assert loaded is not None and loaded.display_name == "Odd\r\x1b[31mRed\x7f"
    assert [s.name for s in registry.list()] == ["odd"]


def test_registry_save_refuses_to_replace_a_good_file_with_invalid_toml(tmp_path, monkeypatch):
    from maestro import agents

    registry = AgentRegistry(tmp_path)
    registry.save(AgentSpec(name="good", kind="codex", display_name="Good"))
    before = (registry.dir / "good.toml").read_text(encoding="utf-8")
    monkeypatch.setattr(agents, "_dump_toml", lambda data: 'name = "unterminated\n')
    with pytest.raises(ValueError, match="invalid TOML"):
        registry.save(AgentSpec(name="good", kind="codex", display_name="Changed"))
    assert (registry.dir / "good.toml").read_text(encoding="utf-8") == before
    assert sorted(p.name for p in registry.dir.iterdir()) == ["good.toml"]


def test_registry_save_writes_owner_only_files(tmp_path):
    registry = AgentRegistry(tmp_path)
    registry.save(AgentSpec(name="secret", kind="codex", token="t0k"))
    path = registry.dir / "secret.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # a new entry is private
    path.chmod(0o644)  # an entry written by an older version
    registry.save(AgentSpec(name="secret", kind="codex", token="t0k2"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # tightened on the next save
    assert registry.get("secret").token == "t0k2"
    assert sorted(p.name for p in registry.dir.iterdir()) == ["secret.toml"]


def test_registry_save_cleans_up_when_the_write_fails(tmp_path, monkeypatch):
    registry = AgentRegistry(tmp_path)

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        registry.save(AgentSpec(name="x", kind="codex"))
    assert list(registry.dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Bug 8: integrations must upgrade stale blocks and write where Codex reads.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "maestro-home"))
    return home


def test_openhands_reinstall_replaces_the_old_block(fake_home):
    from maestro.integrations import INTEGRATIONS

    integration = INTEGRATIONS["openhands"]()
    path = fake_home / ".openhands" / "agent_settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"custom_instructions": "Mine first."}), encoding="utf-8")
    assert integration.install_skill(fake_home, "old skill text").action == "installed"
    result = integration.install_skill(fake_home, "new skill text")
    assert result.ok and result.action == "already-installed"
    text = json.loads(path.read_text(encoding="utf-8"))["custom_instructions"]
    assert "new skill text" in text and "old skill text" not in text
    assert text.startswith("Mine first.") and text.count("maestro-driven-development:begin") == 1


def test_codex_skill_goes_to_agents_md(fake_home):
    from maestro.integrations import INTEGRATIONS

    integration = INTEGRATIONS["codex"]()
    result = integration.install_skill(fake_home, "skill text")
    assert result.ok and result.path == str(fake_home / ".codex" / "AGENTS.md")
    assert "skill text" in (fake_home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert integration.status(fake_home)["installed"] is True


def test_codex_install_moves_a_legacy_block_out_of_instructions_md(fake_home):
    from maestro.integrations import INTEGRATIONS, _managed_block

    legacy = fake_home / ".codex" / "instructions.md"
    legacy.parent.mkdir()
    legacy.write_text(f"My notes.\n\n{_managed_block('old text')}\n", encoding="utf-8")
    integration = INTEGRATIONS["codex"]()
    assert integration.status(fake_home)["installed"] is False
    assert integration.status(fake_home)["legacy_installed"] is True
    integration.install_skill(fake_home, "new text")
    assert legacy.read_text(encoding="utf-8") == "My notes.\n"
    assert integration.status(fake_home)["legacy_installed"] is False


def test_codex_install_deletes_a_legacy_file_that_only_held_the_block(fake_home):
    from maestro.integrations import INTEGRATIONS, _managed_block

    legacy = fake_home / ".codex" / "instructions.md"
    legacy.parent.mkdir()
    legacy.write_text(_managed_block("old") + "\n", encoding="utf-8")
    INTEGRATIONS["codex"]().install_skill(fake_home, "new")
    assert not legacy.exists()


def test_codex_uninstall_removes_the_legacy_block_too(fake_home):
    from maestro.integrations import INTEGRATIONS, SkillManager, _managed_block

    legacy = fake_home / ".codex" / "instructions.md"
    legacy.parent.mkdir()
    legacy.write_text(_managed_block("old") + "\n", encoding="utf-8")
    results = SkillManager(home=fake_home).uninstall()
    assert [(r.kind, r.action) for r in results] == [("codex", "uninstalled")]
    assert not legacy.exists()
    # With nothing left anywhere, uninstall reports a skip.
    assert INTEGRATIONS["codex"]().uninstall_skill(fake_home).action == "skipped"


def test_codex_uninstall_removes_both_blocks(fake_home):
    from maestro.integrations import INTEGRATIONS, _managed_block

    integration = INTEGRATIONS["codex"]()
    integration.install_skill(fake_home, "new")
    legacy = fake_home / ".codex" / "instructions.md"
    legacy.write_text(_managed_block("old") + "\n", encoding="utf-8")
    result = integration.uninstall_skill(fake_home)
    assert result.ok and result.action == "uninstalled"
    assert not legacy.exists() and not (fake_home / ".codex" / "AGENTS.md").exists()


def test_codex_legacy_cleanup_error_is_reported(fake_home, monkeypatch):
    from maestro.integrations import INTEGRATIONS, _managed_block

    legacy = fake_home / ".codex" / "instructions.md"
    legacy.parent.mkdir()
    legacy.write_text(_managed_block("old") + "\n", encoding="utf-8")
    real_unlink = Path.unlink

    def guarded_unlink(self, *args, **kwargs):
        if self == legacy:
            raise OSError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    result = INTEGRATIONS["codex"]().install_skill(fake_home, "new")
    assert result.ok is False and result.action == "error" and "read-only" in result.detail


def test_codex_legacy_unreadable_file_is_not_reported_installed(fake_home):
    from maestro.integrations import INTEGRATIONS

    legacy = fake_home / ".codex" / "instructions.md"
    legacy.parent.mkdir()
    legacy.write_bytes(b"\xff\xfe not utf-8")
    assert INTEGRATIONS["codex"]().status(fake_home)["legacy_installed"] is False


def test_codex_legacy_read_error_is_not_reported_installed(fake_home, monkeypatch):
    from maestro.integrations import INTEGRATIONS, _managed_block

    legacy = fake_home / ".codex" / "instructions.md"
    legacy.parent.mkdir()
    legacy.write_text(_managed_block("old"), encoding="utf-8")
    real_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == legacy:
            raise PermissionError("no access")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    assert INTEGRATIONS["codex"]().status(fake_home)["legacy_installed"] is False


def test_codex_uninstall_reports_a_legacy_cleanup_error(fake_home, monkeypatch):
    from maestro.integrations import INTEGRATIONS, _managed_block

    integration = INTEGRATIONS["codex"]()
    integration.install_skill(fake_home, "new")
    legacy = fake_home / ".codex" / "instructions.md"
    legacy.write_text(_managed_block("old") + "\n", encoding="utf-8")
    real_unlink = Path.unlink

    def guarded_unlink(self, *args, **kwargs):
        if self == legacy:
            raise OSError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    result = integration.uninstall_skill(fake_home)
    assert result.ok is False and result.action == "error" and result.path == str(legacy)
    assert not (fake_home / ".codex" / "AGENTS.md").exists()


def test_codex_uninstall_passes_through_a_read_error(fake_home, monkeypatch):
    from maestro.integrations import INTEGRATIONS

    agents_md = fake_home / ".codex" / "AGENTS.md"
    agents_md.parent.mkdir()
    agents_md.write_text("mine", encoding="utf-8")
    real_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == agents_md:
            raise PermissionError("no access")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    result = INTEGRATIONS["codex"]().uninstall_skill(fake_home)
    assert result.ok is False and result.action == "error" and "no access" in result.detail


# ---------------------------------------------------------------------------
# Bug 9: without a preset fixer, fixes go to the task's actual implementer.
# ---------------------------------------------------------------------------

def test_fixer_defaults_to_the_explicit_target_not_the_preset_implementer():
    from maestro.handoff import HandoffDoc
    from maestro.modes import ModePreset, expand

    preset = ModePreset(name="cheap", implementer="codex-mini", reviewer="reviewer")
    doc = HandoffDoc(title="t", request="r", target_agent="claude", explicit_target=True)
    expand(preset, doc)
    assert doc.target_agent == "claude" and doc.fix_agent == "claude"


# ---------------------------------------------------------------------------
# Review finding 1: a placeholder inside a shell's -c script is shell-quoted.
# A template such as ``sh -c "mytool {prompt}"`` hands the script to a shell,
# so a raw prompt would run its backticks, $(...) and ; as commands. Every
# other argument still receives the value raw, as a single argument.
# ---------------------------------------------------------------------------

_HOSTILE_PROMPT = "fix it; touch pwned $(touch pwned2) `touch pwned3` 'single' \"double\" \\ end"


@pytest.mark.parametrize(
    "template,script_index",
    [
        ('sh -c "mytool --msg {prompt}"', 2),
        ('/bin/bash -lc "mytool --msg {prompt}"', 2),
        ('bash -eo pipefail -c "mytool --msg {prompt}"', 4),
        ('zsh -c -- "mytool --msg {prompt}"', 3),
        ('dash -c -e "mytool --msg {prompt}"', 3),
        ('ksh +x -c "mytool --msg {prompt}"', 3),
        ('bash --rcfile /tmp/rc -c "mytool --msg {prompt}"', 4),
        ('bash --login -c "mytool --msg {prompt}"', 3),
        ('bash -O extglob -c "mytool --msg {prompt}"', 4),
    ],
)
def test_generic_shell_script_placeholder_is_quoted(template, script_index):
    import shlex

    cmd = _generic(template).build_command(_HOSTILE_PROMPT, Path("/w"), "t1", {})
    assert cmd[script_index] == "mytool --msg " + shlex.quote(_HOSTILE_PROMPT)
    assert shlex.split(cmd[script_index]) == ["mytool", "--msg", _HOSTILE_PROMPT]


def test_generic_shell_script_quotes_workspace_and_task_id():
    cmd = _generic('sh -c "cd {workspace} && mytool --id {task_id}"', input_mode="stdin").build_command(
        "ignored", Path("/a b/it's"), "t1", {}
    )
    assert cmd == ["sh", "-c", "cd '/a b/it'\"'\"'s' && mytool --id t1"]


def test_generic_shell_positional_arguments_stay_raw():
    # Words after the script are the script's $0, $1, ...; they are not parsed
    # by the shell, so the value is passed raw as one argument.
    cmd = _generic("""sh -c 'mytool --msg "$1"' sh {prompt}""").build_command(_HOSTILE_PROMPT, Path("/w"), "t1", {})
    assert cmd == ["sh", "-c", 'mytool --msg "$1"', "sh", _HOSTILE_PROMPT]


def test_generic_shell_without_c_flag_passes_values_raw():
    cmd = _generic("bash ./run.sh --msg={prompt} {workspace}").build_command(_HOSTILE_PROMPT, Path("/a b"), "t1", {})
    assert cmd == ["bash", "./run.sh", f"--msg={_HOSTILE_PROMPT}", "/a b"]


def test_generic_non_shell_program_passes_values_raw():
    cmd = _generic("mytool -c {prompt} --msg={prompt}").build_command(_HOSTILE_PROMPT, Path("/w"), "t1", {})
    assert cmd == ["mytool", "-c", _HOSTILE_PROMPT, f"--msg={_HOSTILE_PROMPT}"]


def test_generic_shell_with_c_flag_but_no_script_word():
    assert _generic("sh -c").build_command("p", Path("/w"), "t1", {}) == ["sh", "-c"]
    assert _generic("sh -c --").build_command("p", Path("/w"), "t1", {}) == ["sh", "-c", "--"]


def test_generic_shell_arguments_after_double_dash_without_c_stay_raw():
    cmd = _generic("bash -- ./run.sh {prompt}").build_command(_HOSTILE_PROMPT, Path("/w"), "t1", {})
    assert cmd == ["bash", "--", "./run.sh", _HOSTILE_PROMPT]


def test_generic_fish_script_uses_fish_quoting():
    prompt = "a\\'; touch pwned; echo 'b"
    cmd = _generic('fish -c "mytool {prompt}"').build_command(prompt, Path("/w"), "t1", {})
    assert cmd == ["fish", "-c", "mytool 'a\\\\\\'; touch pwned; echo \\'b'"]
    long_form = _generic('fish --command="mytool {prompt}"').build_command("x y", Path("/w"), "t1", {})
    assert long_form == ["fish", "--command=mytool 'x y'"]
    separate = _generic('fish --login --command "mytool {prompt}"').build_command("x y", Path("/w"), "t1", {})
    assert separate == ["fish", "--login", "--command", "mytool 'x y'"]
    init = _generic('fish -C "set x 1" -c "mytool {prompt}"').build_command("x y", Path("/w"), "t1", {})
    assert init == ["fish", "-C", "set x 1", "-c", "mytool 'x y'"]


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash", "ksh"])
def test_generic_shell_script_runs_without_injection(tmp_path, shell):
    """End to end: the shell receives the prompt as text and runs nothing from it."""
    import shutil as _shutil

    if _shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out = tmp_path / "out.txt"
    adapter = _generic(f"{shell} -c \"printf '%s' {{prompt}} > '{out}'\"")
    result = adapter.run(_HOSTILE_PROMPT, workspace, "t1", settings={}, timeout=30, log_dir=tmp_path / "logs")
    assert result.ok, result.error
    assert out.read_text(encoding="utf-8") == _HOSTILE_PROMPT
    assert sorted(p.name for p in workspace.iterdir()) == []


# ---------------------------------------------------------------------------
# Review finding 2: per-task model and effort reach only the task's target.
# Fallback agents, gate agents and a separate fixer run with their own
# registry settings, and [defaults] never overrides a registry value.
# ---------------------------------------------------------------------------

@pytest.fixture
def maestro_daemon(tmp_path, monkeypatch):
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "maestro-home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    yield d
    d.stop()


def _task_doc(**kw):
    from maestro.handoff import HandoffDoc

    base = dict(title="t", request="r", verification="none", commit_policy="no-commit", target_agent="codex", explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


def test_turn_settings_give_other_agents_their_registry_model_and_effort():
    from maestro.daemon import MaestroDaemon

    doc = _task_doc(agent_settings={"model": "gpt-task", "effort": "max", "extra": 1})
    cursor = AgentSpec(name="cursor", kind="cursor", model="sonnet-4")
    settings = MaestroDaemon._turn_settings(cursor, doc)
    assert settings["model"] == "sonnet-4" and "effort" not in settings
    assert settings["extra"] == 1  # other per-task keys still reach every agent
    cline = AgentSpec(name="cline", kind="cline", effort="high")
    cmd = ClineAdapter(cline).build_command("p", Path("/w"), "t1", MaestroDaemon._turn_settings(cline, doc))
    assert cmd[cmd.index("--thinking") + 1] == "high"


def test_turn_settings_let_the_task_override_win_for_the_target():
    from maestro.daemon import MaestroDaemon

    doc = _task_doc(agent_settings={"model": "gpt-task", "effort": "max"})
    codex = AgentSpec(name="codex", kind="codex", model="gpt-registry", effort="low")
    settings = MaestroDaemon._turn_settings(codex, doc)
    assert settings["model"] == "gpt-task" and settings["effort"] == "max"


def test_cline_unknown_task_effort_falls_back_to_registry_effort():
    adapter = ClineAdapter(AgentSpec(name="cline", kind="cline", effort="medium"))
    cmd = adapter.build_command("p", Path("/w"), "t1", {"effort": "max"})
    assert cmd[cmd.index("--thinking") + 1] == "medium"
    bare = ClineAdapter(AgentSpec(name="cline", kind="cline"))
    assert "--thinking" not in bare.build_command("p", Path("/w"), "t1", {"effort": "max"})
    assert "--thinking" not in ClineAdapter(None).build_command("p", Path("/w"), "t1", {"effort": "max"})


def test_defaults_do_not_override_the_target_registry_model_or_effort(maestro_daemon):
    maestro_daemon.registry.save(AgentSpec(name="codex", kind="codex", model="gpt-registry", effort="low"))
    maestro_daemon.maestro.config["defaults"] = {"agent": "codex", "model": "gpt-default", "effort": "max"}
    doc = _task_doc(target_agent="codex", explicit_target=False)
    assert maestro_daemon._apply_defaults(doc) is True
    assert doc.agent_settings == {}
    settings = maestro_daemon._turn_settings(maestro_daemon.registry.get("codex"), doc)
    assert settings["model"] == "gpt-registry" and settings["effort"] == "low"


def test_defaults_fill_the_target_when_its_registry_sets_nothing(maestro_daemon):
    maestro_daemon.maestro.config["defaults"] = {"agent": "codex", "model": "gpt-default", "effort": "max"}
    doc = _task_doc(target_agent="codex", explicit_target=False)
    maestro_daemon._apply_defaults(doc)
    assert doc.agent_settings == {"model": "gpt-default", "effort": "max"}


def test_fallback_agent_runs_with_its_own_registry_model(maestro_daemon, tmp_path, monkeypatch):
    """End to end: the target fails, and the Cursor fallback keeps its model."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    args_file = tmp_path / "cursor-args.txt"
    for name, body in (
        ("codex", "cat > /dev/null\nexit 1"),
        ("cursor-agent", f'printf "%s\\n" "$@" > "{args_file}"\ncat > /dev/null\necho "{{}}"\nexit 0'),
    ):
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    maestro_daemon.registry.save(AgentSpec(name="cursor", kind="cursor", model="sonnet-4"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    doc = _task_doc(fallback=["cursor"], agent_settings={"model": "gpt-task"})
    started = maestro_daemon.delegate(doc, workspace)
    maestro_daemon.wait(started["task_id"], timeout=30)
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--model") + 1] == "sonnet-4"


# ---------------------------------------------------------------------------
# Review finding 3: the daemon checks skill paths against the task workspace.
# ---------------------------------------------------------------------------

def test_delegate_resolves_relative_skill_paths_against_the_workspace(maestro_daemon, tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    _skill_dir(workspace / "skills" / "pdf")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    doc = _task_doc(target_agent="nobody-installed", context_entries=[{"label": "pdf", "kind": "skill", "path": "skills/pdf"}])
    started = maestro_daemon.delegate(doc, workspace)
    maestro_daemon.wait(started["task_id"], timeout=30)
    # A skill that exists only relative to the daemon's working directory is refused.
    _skill_dir(cwd / "skills" / "other")
    other = _task_doc(context_entries=[{"label": "other", "kind": "skill", "path": "skills/other"}])
    with pytest.raises(ValueError, match="directory not found"):
        maestro_daemon.delegate(other, workspace)


# ---------------------------------------------------------------------------
# Review finding 4: an instructions file that is not UTF-8 is never
# overwritten, is reported by status, and does not break other integrations.
# ---------------------------------------------------------------------------

def test_block_integration_refuses_a_file_that_is_not_utf8(fake_home):
    from maestro.integrations import INTEGRATIONS

    path = fake_home / ".codex" / "AGENTS.md"
    path.parent.mkdir()
    original = b"caf\xe9 notes written in Latin-1\n"
    path.write_bytes(original)
    integration = INTEGRATIONS["codex"]()
    state = integration.status(fake_home)
    assert state["installed"] is False and "not valid UTF-8" in state["error"]
    installed = integration.install_skill(fake_home, "skill text")
    assert installed.ok is False and installed.action == "error" and "not valid UTF-8" in installed.detail
    removed = integration.uninstall_skill(fake_home)
    assert removed.ok is False and removed.action == "error" and "not valid UTF-8" in removed.detail
    assert path.read_bytes() == original


def test_status_reports_an_unreadable_instructions_file(fake_home, monkeypatch):
    from maestro.integrations import INTEGRATIONS

    path = fake_home / ".copilot" / "instructions.md"
    path.parent.mkdir()
    path.write_text("mine", encoding="utf-8")
    real_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == path:
            raise PermissionError("no access")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    state = INTEGRATIONS["copilot"]().status(fake_home)
    assert state["installed"] is False and "no access" in state["error"]


def test_manager_uninstall_survives_a_file_that_is_not_utf8(fake_home):
    from maestro.integrations import INTEGRATIONS, SkillManager

    INTEGRATIONS["claude_code"]().install_skill(fake_home, "skill text")
    path = fake_home / ".codex" / "AGENTS.md"
    path.parent.mkdir()
    path.write_bytes(b"\xff\xfe not utf-8")
    manager = SkillManager(home=fake_home)
    assert "error" in {entry["kind"]: entry for entry in manager.status()}["codex"]
    results = {r.kind: r for r in manager.uninstall()}
    assert results["claude_code"].ok and results["claude_code"].action == "uninstalled"
    assert results["codex"].ok is False and "not valid UTF-8" in results["codex"].detail
    assert path.read_bytes() == b"\xff\xfe not utf-8"


# ---------------------------------------------------------------------------
# Review finding 5: max_depth_remaining must be a real integer.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [0.5, 2.0, True, "2", [3]])
def test_from_dict_rejects_a_max_depth_that_is_not_an_integer(value):
    from maestro.handoff import from_dict

    with pytest.raises(ValueError, match="max_depth_remaining must be an integer"):
        from_dict(_payload(constraints={"max_depth_remaining": value}))


def test_handoff_documents_written_by_maestro_still_load():
    from maestro.handoff import from_dict, from_toml, to_toml

    doc = _task_doc(max_depth_remaining=2)
    assert from_dict(json.loads(json.dumps(doc.to_dict()))).max_depth_remaining == 2
    assert from_toml(to_toml(doc)).max_depth_remaining == 2
    assert from_dict(_payload()).max_depth_remaining == 3


# ---------------------------------------------------------------------------
# Review finding 6: save() removes its temporary file on any failure.
# ---------------------------------------------------------------------------

def test_registry_save_cleans_up_when_the_text_cannot_be_encoded(tmp_path):
    registry = AgentRegistry(tmp_path)
    with pytest.raises(UnicodeEncodeError):
        registry.save(AgentSpec(name="x", kind="codex", token="secret\ud800token"))
    assert list(registry.dir.iterdir()) == []
