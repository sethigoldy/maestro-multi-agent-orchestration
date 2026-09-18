from __future__ import annotations

import json

import pytest

from maestro.handoff import (
    COMMIT_POLICIES,
    HandoffDoc,
    from_dict,
    from_legacy,
    from_toml,
    load_handoff_file,
    to_toml,
    validate_handoff,
)


def _doc(**overrides) -> HandoffDoc:
    base = dict(title="Add search", request="Implement semantic search")
    base.update(overrides)
    return HandoffDoc(**base)


def test_default_document_is_valid():
    doc = validate_handoff(_doc())
    assert doc.target_agent == "codex" and doc.origin_agent == "human"
    assert doc.commit_policy == "branch" and doc.verification == "auto"
    assert doc.max_depth_remaining == 3 and doc.sensitive is False


def test_to_dict_has_four_sections_plus_settings():
    data = _doc(fallback=["claude_code"], budget_hint=2.5, agent_settings={"model": "m"}).to_dict()
    assert set(data) == {"handoff", "routing", "expectations", "constraints", "agent_settings"}
    assert data["routing"]["target_agent"] == "codex" and data["routing"]["fallback"] == ["claude_code"]
    assert data["expectations"]["budget_hint"] == 2.5
    assert data["agent_settings"] == {"model": "m"}


def test_from_dict_roundtrip():
    doc = _doc(title="T", request="R", design="D", context_files=["a.py"], context_notes="n",
               target_agent="pi", fallback=["codex"], origin_agent="claude_code", parent_task_id="task-x",
               artifacts=["code", "tests"], verification="none", commit_policy="no-commit",
               budget_hint=1.0, sensitive=True, max_depth_remaining=1)
    restored = from_dict(doc.to_dict())
    assert restored.title == "T" and restored.request == "R" and restored.design == "D"
    assert restored.context_files == ["a.py"] and restored.target_agent == "pi"
    assert restored.fallback == ["codex"] and restored.origin_agent == "claude_code"
    assert restored.parent_task_id == "task-x" and restored.artifacts == ["code", "tests"]
    assert restored.verification == "none" and restored.commit_policy == "no-commit"
    assert restored.budget_hint == 1.0 and restored.sensitive is True and restored.max_depth_remaining == 1


def test_validation_errors():
    for bad in (
        dict(title=""),
        dict(request="   "),
        dict(target_agent=""),
        dict(commit_policy="yolo"),
        dict(verification="maybe"),
        dict(max_depth_remaining=-1),
        dict(budget_hint=0),
        dict(fallback=["ok", "  "]),
    ):
        with pytest.raises(ValueError):
            validate_handoff(_doc(**bad))


def test_commit_policies_constant():
    assert COMMIT_POLICIES == ("no-commit", "branch", "pr")


def test_toml_roundtrip(tmp_path):
    doc = _doc(title="T\nwith newline", request="R", design="D", target_agent="cline")
    text = to_toml(doc)
    restored = from_toml(text)
    assert restored.title == "T\nwith newline" and restored.request == "R" and restored.design == "D"
    assert restored.target_agent == "cline"


def test_from_toml_invalid():
    with pytest.raises(ValueError):
        from_toml("not toml ===")


def test_from_legacy(tmp_path):
    design = tmp_path / "design.md"
    design.write_text("# design\nbody", encoding="utf-8")
    doc = from_legacy({"title": "T", "request": "R", "design_file": str(design), "model": "gpt-x", "effort": "high"})
    assert doc.design == "# design\nbody" and doc.target_agent == "codex"
    # No supervisor field in the old file: record the honest generic default.
    assert doc.origin_agent == "human" and doc.commit_policy == "branch"
    assert doc.agent_settings == {"model": "gpt-x", "effort": "high"}


def test_from_legacy_explicit_supervisor(tmp_path):
    doc = from_legacy({"title": "T", "request": "R", "supervisor": "copilot"})
    assert doc.origin_agent == "copilot" and doc.target_agent == "codex"


def test_from_legacy_missing_fields():
    with pytest.raises(ValueError):
        from_legacy({"title": "T"})
    with pytest.raises(ValueError):
        from_legacy("nope")


def test_load_handoff_file_shapes(tmp_path):
    ws = tmp_path

    four_section = ws / "a.json"
    four_section.write_text(json.dumps(_doc().to_dict()), encoding="utf-8")
    assert load_handoff_file(four_section).title == "Add search"

    legacy = ws / "b.json"
    legacy.write_text(json.dumps({"title": "L", "request": "LR"}), encoding="utf-8")
    doc = load_handoff_file(legacy)
    assert doc.title == "L" and doc.request == "LR" and doc.target_agent == "codex"

    toml_doc = ws / "c.toml"
    toml_doc.write_text(to_toml(_doc(title="TOML DOC")), encoding="utf-8")
    assert load_handoff_file(toml_doc).title == "TOML DOC"

    with pytest.raises(ValueError):
        load_handoff_file(ws / "missing.json")

    garbage = ws / "d.json"
    garbage.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_handoff_file(garbage)


def test_section_must_be_table():
    with pytest.raises(ValueError, match="table"):
        from_dict({"handoff": ["not", "a", "table"], "routing": {}, "expectations": {}, "constraints": {}})


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="mapping"):
        from_dict(["nope"])  # type: ignore[arg-type]


def test_load_handoff_file_rejects_json_array(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        load_handoff_file(p)


# ------------------------------------------------------------------ work modes
def test_work_mode_fields_round_trip_dict():
    doc = _doc(mode="economy", review_agent="rev", verify_agent="ver", fix_agent="fix", max_bounces=3, explicit_target=True)
    data = doc.to_dict()
    routing = data["routing"]
    assert (routing["mode"], routing["review_agent"], routing["verify_agent"], routing["fix_agent"], routing["max_bounces"]) == ("economy", "rev", "ver", "fix", 3)
    back = from_dict(data)
    assert (back.mode, back.review_agent, back.verify_agent, back.fix_agent, back.max_bounces) == ("economy", "rev", "ver", "fix", 3)
    assert back.explicit_target is True


def test_work_mode_fields_absent_default_to_none():
    doc = from_dict({"handoff": {"title": "t", "request": "r"}, "routing": {}, "expectations": {}, "constraints": {}})
    assert (doc.mode, doc.review_agent, doc.verify_agent, doc.fix_agent, doc.max_bounces) == (None, None, None, None, None)
    assert doc.explicit_target is False


def test_work_mode_fields_toml_round_trip():
    doc = _doc(mode="economy", review_agent="rev", max_bounces=1)
    back = from_toml(to_toml(doc))
    assert (back.mode, back.review_agent, back.verify_agent, back.fix_agent, back.max_bounces) == ("economy", "rev", None, None, 1)


def test_max_bounces_validation():
    with pytest.raises(ValueError, match="max_bounces"):
        validate_handoff(_doc(max_bounces=-1))
    with pytest.raises(ValueError, match="max_bounces"):
        validate_handoff(_doc(max_bounces=True))  # bool is not an int here
    assert validate_handoff(_doc(max_bounces=0)).max_bounces == 0
