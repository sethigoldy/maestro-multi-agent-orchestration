from __future__ import annotations

from pathlib import Path

import pytest

from maestro.context import (
    FILE_INLINE_LIMIT,
    PHASES,
    TOTAL_CONTEXT_LIMIT,
    ContextEntry,
    compose_context,
    entry_from_dict,
    parse_context_config,
    parse_entry,
    render_context,
)


def _entry(label="a", kind="text", text="hello", path=None, phases=PHASES, source="handoff") -> ContextEntry:
    return ContextEntry(label=label, kind=kind, text=text, path=path, phases=phases, source=source)


# ------------------------------------------------------------------ parse_entry
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"label": "a", "text": "t"}, ("text", "t", None, PHASES)),
        ({"label": "a", "path": "p.md"}, ("file", None, "p.md", PHASES)),
        ({"label": "a", "kind": "skill", "path": "~/skills/s"}, ("skill", None, "~/skills/s", PHASES)),
        ({"label": "a", "text": "t", "phases": ["reviewer"]}, ("text", "t", None, ("reviewer",))),
        ({"label": "  spaced  ", "text": "t"}, ("text", "t", None, PHASES)),
    ],
)
def test_parse_entry_valid(raw, expected):
    entry = parse_entry(raw, source="project config")
    assert (entry.kind, entry.text, entry.path, entry.phases) == expected
    assert entry.label.strip() and entry.source == "project config"


@pytest.mark.parametrize(
    "raw",
    [
        "not a table",
        {},
        {"label": ""},
        {"text": "t"},  # missing label
        {"label": "x"},  # neither text nor path
        {"label": "x", "kind": "magic", "text": "t"},
        {"label": "x", "kind": "skill"},  # skill without path
        {"label": "x", "kind": "file"},  # file without path
        {"label": "x", "kind": "file", "path": "p", "text": "t"},  # both set
        {"label": "x", "kind": "text", "text": "t", "path": "p"},  # both set
        {"label": "x", "kind": "text", "text": "   "},  # blank text
        {"label": "x", "kind": "text", "text": 5},  # non-string text
        {"label": "x", "kind": "file", "path": 3},  # non-string path
        {"label": "x", "text": "t", "phases": ["supervisor"]},
        {"label": "x", "text": "t", "phases": []},
        {"label": "x", "text": "t", "phases": "implementer"},
    ],
)
def test_parse_entry_invalid(raw):
    with pytest.raises(ValueError):
        parse_entry(raw)


def test_parse_entry_error_names_label():
    with pytest.raises(ValueError, match="magic-label"):
        parse_entry({"label": "magic-label", "kind": "nope", "text": "t"})


# ------------------------------------------------------------------ to_dict / entry_from_dict
def test_entry_to_dict_round_trip():
    full = _entry(label="pdf", kind="skill", text=None, path="~/skills/pdf", phases=("implementer",), source="user config")
    restored = entry_from_dict(full.to_dict())
    assert restored == full


def test_entry_to_dict_omits_defaults():
    data = _entry().to_dict()
    assert data == {"label": "a", "kind": "text", "text": "hello", "source": "handoff"}
    assert "phases" not in data and "path" not in data


def test_entry_to_dict_omits_empty_source():
    data = ContextEntry(label="a", kind="text", text="t").to_dict()
    assert data == {"label": "a", "kind": "text", "text": "t"}


# ------------------------------------------------------------------ parse_context_config
def test_parse_context_config_none_and_empty():
    assert parse_context_config(None, "project config") == {}
    assert parse_context_config({}, "project config") == {}


def test_parse_context_config_labels_are_keys():
    entries = parse_context_config(
        {"style": {"text": "be brief"}, "pdf": {"kind": "skill", "path": "~/skills/pdf"}},
        source="user config",
    )
    assert set(entries) == {"style", "pdf"}
    assert entries["style"].label == "style" and entries["style"].source == "user config"
    assert entries["pdf"].kind == "skill"


def test_parse_context_config_errors():
    with pytest.raises(ValueError, match="must be a table of label-keyed"):
        parse_context_config(["nope"], "project config")
    with pytest.raises(ValueError, match="must be a table"):
        parse_context_config({"x": "scalar"}, "project config")
    with pytest.raises(ValueError, match="conflicts with the table key"):
        parse_context_config({"x": {"label": "y", "text": "t"}}, "project config")


# ------------------------------------------------------------------ compose_context
def test_compose_context_order_and_override():
    config = {
        "alpha": _entry(label="alpha", source="user config"),
        "beta": _entry(label="beta", text="standing", source="project config"),
    }
    handoff = [
        {"label": "beta", "kind": "text", "text": "task-specific"},  # overrides standing beta
        {"label": "gamma", "kind": "text", "text": "new"},
    ]
    composed = compose_context(config, handoff)
    assert [e.label for e in composed] == ["alpha", "beta", "gamma"]
    by_label = {e.label: e for e in composed}
    assert by_label["beta"].text == "task-specific" and by_label["beta"].source == "handoff"
    assert by_label["alpha"].source == "user config"
    assert by_label["gamma"].source == "handoff"


def test_compose_context_no_handoff():
    config = {"a": _entry(label="a", source="project config")}
    composed = compose_context(config, [])
    assert [e.label for e in composed] == ["a"] and composed[0].source == "project config"


def test_compose_context_validates_handoff_entries():
    with pytest.raises(ValueError):
        compose_context({}, [{"label": ""}])


# ------------------------------------------------------------------ render_context
def test_render_context_empty():
    rendered = render_context([], "implementer", Path("/ws"), Path("/task"), "codex")
    assert rendered.block == "" and rendered.system_file is None and rendered.skills_root is None


def test_render_context_text_block_shape():
    rendered = render_context(
        [_entry(label="style", text="be brief", source="project config")],
        "implementer", Path("/ws"), Path("/task"), "codex",
    )
    assert rendered.block.startswith("CONTEXT (user-provided; follow these along with the request):")
    assert "[style] (project config)\nbe brief\n" in rendered.block
    assert rendered.system_file is None and rendered.skills_root is None


def test_render_context_phase_filtering(tmp_path):
    entries = [
        _entry(label="all", text="everywhere"),
        _entry(label="review-only", text="checklist", phases=("reviewer",)),
    ]
    impl = render_context(entries, "implementer", Path("/ws"), tmp_path / "t1", "codex")
    assert "[all]" in impl.block and "checklist" not in impl.block
    rev = render_context(entries, "reviewer", Path("/ws"), tmp_path / "t2", "codex")
    assert "[all]" in rev.block and "[review-only] (handoff)\nchecklist\n" in rev.block


def test_render_context_file_inline_and_artifact(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    small = ws / "small.md"
    small.write_text("tiny", encoding="utf-8")
    big = ws / "big.md"
    big.write_text("x" * (FILE_INLINE_LIMIT + 1), encoding="utf-8")
    entries = [
        _entry(label="small", kind="file", text=None, path="small.md"),
        _entry(label="big", kind="file", text=None, path="big.md"),
    ]
    task_dir = tmp_path / "task"
    rendered = render_context(entries, "implementer", ws, task_dir, "codex")
    assert "[small] (handoff)\ntiny\n" in rendered.block
    artifact = task_dir / "context-big.txt"
    assert artifact.is_file() and artifact.read_text(encoding="utf-8") == "x" * (FILE_INLINE_LIMIT + 1)
    assert f"(file too large to inline; full content at {artifact})" in rendered.block


def test_render_context_file_missing_degrades(tmp_path):
    rendered = render_context(
        [_entry(label="ghost", kind="file", text=None, path="nope.md")],
        "implementer", tmp_path / "ws", tmp_path / "task", "codex",
    )
    assert "(file unreadable: " in rendered.block and "[ghost] (handoff)" in rendered.block


def test_render_context_skill_staging(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    skill = tmp_path / "skills" / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\n---\nbody", encoding="utf-8")
    task_dir = tmp_path / "task"
    rendered = render_context(
        [_entry(label="pdf", kind="skill", text=None, path=str(skill))],
        "implementer", ws, task_dir, "codex",
    )
    staged = task_dir / "context" / "skills" / ".claude" / "skills" / "pdf"
    assert (staged / "SKILL.md").is_file()
    assert rendered.skills_root == task_dir / "context" / "skills"
    assert f'Skill "pdf" is available at {staged}' in rendered.block


def test_render_context_skill_restage_updates_bytes(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("v1", encoding="utf-8")
    task_dir = tmp_path / "task"
    entry = _entry(label="s", kind="skill", text=None, path=str(skill))
    first = render_context([entry], "implementer", tmp_path, task_dir, "codex")
    (skill / "SKILL.md").write_text("v2", encoding="utf-8")
    second = render_context([entry], "implementer", tmp_path, task_dir, "codex")
    staged = first.skills_root / ".claude" / "skills" / "s" / "SKILL.md"
    assert staged.read_text(encoding="utf-8") == "v2"


def test_render_context_skill_label_slug(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    task_dir = tmp_path / "task"
    rendered = render_context(
        [_entry(label="pdf processing!", kind="skill", text=None, path=str(skill))],
        "implementer", tmp_path, task_dir, "codex",
    )
    assert (rendered.skills_root / ".claude" / "skills" / "pdf-processing" / "SKILL.md").is_file()


def test_render_context_total_cap_drops_entries(tmp_path):
    entries = [
        _entry(label="huge", text="x" * (TOTAL_CONTEXT_LIMIT + 1)),
        _entry(label="tiny", text="y"),
    ]
    rendered = render_context(entries, "implementer", tmp_path / "ws", tmp_path / "task", "codex")
    assert "x" * 100 not in rendered.block  # the huge body never renders
    assert "[tiny] (handoff)\ny\n" in rendered.block  # later small entries still fit
    assert "were not included: huge" in rendered.block


def test_render_context_cap_keeps_fitting_prefix(tmp_path):
    entries = [
        _entry(label="first", text="x" * (TOTAL_CONTEXT_LIMIT - 100)),
        _entry(label="second", text="y" * 200),
    ]
    rendered = render_context(entries, "implementer", tmp_path / "ws", tmp_path / "task", "codex")
    assert "[first]" in rendered.block and "second" not in rendered.block.replace("were not included: second", "")


# ------------------------------------------------------------------ claude_code channel split (D1)
def test_render_context_claude_code_system_channel(tmp_path):
    entries = [
        _entry(label="standing", text="repo conventions", source="project config"),
        _entry(label="task", text="for this task only", source="handoff"),
    ]
    task_dir = tmp_path / "task"
    rendered = render_context(entries, "implementer", tmp_path / "ws", task_dir, "claude_code")
    assert rendered.system_file == task_dir / "context-system.md"
    system_text = rendered.system_file.read_text(encoding="utf-8")
    assert "[standing] (project config)\nrepo conventions\n" in system_text
    assert "task only" not in system_text
    assert "[task] (handoff)\nfor this task only\n" in rendered.block
    assert "repo conventions" not in rendered.block


def test_render_context_other_adapters_get_everything_in_block(tmp_path):
    entries = [_entry(label="standing", text="repo conventions", source="project config")]
    rendered = render_context(entries, "implementer", tmp_path / "ws", tmp_path / "task", "codex")
    assert rendered.system_file is None
    assert "[standing] (project config)\nrepo conventions\n" in rendered.block


def test_render_context_claude_code_skill_stays_in_block(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    task_dir = tmp_path / "task"
    rendered = render_context(
        [_entry(label="s", kind="skill", text=None, path=str(skill), source="project config")],
        "implementer", tmp_path / "ws", task_dir, "claude_code",
    )
    assert rendered.system_file is None  # skills are not system-prompt content
    assert 'Skill "s" is available at' in rendered.block
