from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from maestro.agents import (
    AgentRegistry,
    AgentSpec,
    BUILTIN_ADAPTERS,
    GENERIC_KIND,
    _dump_toml,
    _probe_version,
    _spec_from_data,
    _toml_scalar,
    validate_agent_spec,
)


def _builtin(name="codex") -> AgentSpec:
    return AgentSpec(name=name, kind="codex", display_name="Codex", skills=["implementation"])


def test_builtin_kinds_and_constants():
    from maestro.agents import DEFAULT_BINARIES, INPUT_MODES, OUTPUT_FORMATS, WORKSPACE_POLICIES

    assert GENERIC_KIND not in BUILTIN_ADAPTERS
    assert set(DEFAULT_BINARIES) == set(BUILTIN_ADAPTERS)
    assert "arg" in INPUT_MODES and "jsonl" in OUTPUT_FORMATS and "cwd" in WORKSPACE_POLICIES


def test_validate_builtin_ok():
    spec = validate_agent_spec(_builtin())
    assert spec.kind == "codex" and spec.enabled is True


def test_validate_rejects_bad_name():
    for bad in ("Bad_Name", "", "has space", "-leading"):
        with pytest.raises(ValueError):
            validate_agent_spec(AgentSpec(name=bad, kind="codex"))


def test_validate_rejects_unknown_kind_and_source():
    with pytest.raises(ValueError):
        validate_agent_spec(AgentSpec(name="a", kind="nope"))
    with pytest.raises(ValueError):
        validate_agent_spec(AgentSpec(name="a", kind="codex", source="alien"))


def test_validate_rejects_bad_skills():
    with pytest.raises(ValueError):
        validate_agent_spec(AgentSpec(name="a", kind="codex", skills=["  "]))


def test_validate_generic_rules():
    spec = AgentSpec(name="mycli", kind=GENERIC_KIND, command="mycli --run {prompt}")
    assert validate_agent_spec(spec).command == "mycli --run {prompt}"
    for field, value in (("command", None), ("command", "   "), ("input_mode", "nope"),
                         ("output_format", "nope"), ("workspace_policy", "nope")):
        bad = AgentSpec(name="mycli", kind=GENERIC_KIND, command="x")
        setattr(bad, field, value)
        with pytest.raises(ValueError):
            validate_agent_spec(bad)


def test_toml_scalar_types():
    assert _toml_scalar(True) == "true" and _toml_scalar(False) == "false"
    assert _toml_scalar(3) == "3" and _toml_scalar(1.5) == "1.5"
    assert _toml_scalar('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'
    with pytest.raises(TypeError):
        _toml_scalar(["list"])


def test_dump_toml_flat_and_nested():
    text = _dump_toml({"name": "x", "enabled": False, "count": 2, "tags": ["a", "b"], "meta": {"k": "v", "n": 1}})
    assert 'name = "x"' in text and "enabled = false" in text and "count = 2" in text
    assert 'tags = ["a", "b"]' in text and "[meta]" in text and 'k = "v"' in text and "n = 1" in text


def test_dump_toml_nested_table_with_list():
    text = _dump_toml({"meta": {"tags": ["a", "b"], "flag": True}})
    assert "[meta]" in text and 'tags = ["a", "b"]' in text and "flag = true" in text


def test_dump_toml_rejects_non_string_list_items():
    with pytest.raises(TypeError):
        _dump_toml({"items": [1, 2]})


def test_spec_to_dict_builtin_omits_generic_fields():
    data = _builtin().to_dict()
    assert data == {"name": "codex", "kind": "codex", "display_name": "Codex",
                    "skills": ["implementation"], "enabled": True, "source": "user"}


def test_spec_to_dict_generic_includes_config():
    spec = AgentSpec(name="g", kind=GENERIC_KIND, command="g --x {prompt}", input_mode="stdin",
                     output_format="jsonl", workspace_policy="flag")
    data = spec.to_dict()
    assert data["command"] == "g --x {prompt}" and data["input_mode"] == "stdin"
    assert data["output_format"] == "jsonl" and data["workspace_policy"] == "flag"


def test_spec_from_data_defaults_and_unknown_keys():
    spec = _spec_from_data({"name": "a", "kind": "codex"})
    assert spec.display_name == "" and spec.skills == [] and spec.enabled is True
    with pytest.raises(ValueError):
        _spec_from_data({"name": "a", "kind": "codex", "bogus": 1})


def test_registry_save_get_roundtrip(tmp_path):
    reg = AgentRegistry(tmp_path)
    reg.save(_builtin())
    got = reg.get("codex")
    assert got is not None and got.display_name == "Codex" and got.skills == ["implementation"]
    assert reg.get("missing") is None


def test_registry_generic_roundtrip(tmp_path):
    reg = AgentRegistry(tmp_path)
    spec = AgentSpec(name="mycli", kind=GENERIC_KIND, command="mycli --run {prompt} {workspace}")
    reg.save(spec)
    got = reg.get("mycli")
    assert got is not None and got.command == "mycli --run {prompt} {workspace}"


def test_registry_list_skips_corrupt_files(tmp_path):
    reg = AgentRegistry(tmp_path)
    reg.save(_builtin("codex"))
    (reg.dir / "broken.toml").write_text("this is not toml ===", encoding="utf-8")
    (reg.dir / "badspec.toml").write_text('name = "x"\nkind = "nope"\n', encoding="utf-8")
    names = [s.name for s in reg.list()]
    assert names == ["codex"]


def test_registry_remove(tmp_path):
    reg = AgentRegistry(tmp_path)
    reg.save(_builtin())
    assert reg.remove("codex") is True and reg.get("codex") is None
    assert reg.remove("codex") is False


def test_registry_rejects_bad_name_in_path(tmp_path):
    reg = AgentRegistry(tmp_path)
    with pytest.raises(ValueError):
        reg.get("Bad Name")


def _fake_bin(bindir: Path, name: str, body: str = "echo '{name} 1.2.3'") -> Path:
    path = bindir / name
    path.write_text(f"#!/bin/sh\n{body.replace('{name}', name)}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_discover_scans_path(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "codex")
    _fake_bin(bindir, "pi")
    monkeypatch.setenv("PATH", str(bindir))
    reg = AgentRegistry(tmp_path / "state")
    found = {c["name"]: c for c in reg.discover()}
    assert found["codex"]["found"] is True and found["codex"]["path"] == str(bindir / "codex")
    assert found["pi"]["found"] is True
    assert found["claude_code"]["found"] is False and found["claude_code"]["path"] is None


def test_status_unregistered(tmp_path):
    reg = AgentRegistry(tmp_path)
    assert reg.status("ghost") == {"name": "ghost", "registered": False}


def test_status_builtin_found(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "codex")
    monkeypatch.setenv("PATH", str(bindir))
    reg = AgentRegistry(tmp_path)
    reg.save(_builtin())
    status = reg.status("codex")
    assert status["registered"] is True and status["found"] is True
    assert status["version"] == "codex 1.2.3" and status["binary"] == "codex"


def test_status_builtin_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    reg = AgentRegistry(tmp_path)
    reg.save(_builtin())
    status = reg.status("codex")
    assert status["found"] is False and status["version"] is None


def test_status_generic_uses_command_binary(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "mycli", body="echo 'mycli 0.1'")
    monkeypatch.setenv("PATH", str(bindir))
    reg = AgentRegistry(tmp_path)
    reg.save(AgentSpec(name="g", kind=GENERIC_KIND, command="mycli --run {prompt}"))
    status = reg.status("g")
    assert status["binary"] == "mycli" and status["found"] is True and status["version"] == "mycli 0.1"


def test_status_a2a_remote_has_no_binary(tmp_path):
    reg = AgentRegistry(tmp_path)
    reg.save(AgentSpec(name="remote", kind="a2a_remote"))
    status = reg.status("remote")
    assert status["binary"] is None and status["found"] is False and status["version"] is None


def test_probe_version_none_and_errors(tmp_path):
    assert _probe_version(None) is None
    blocked = tmp_path / "blocked"
    blocked.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")  # not executable
    assert _probe_version(str(blocked)) is None


def test_probe_version_empty_and_multiline(tmp_path):
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    silent.chmod(silent.stat().st_mode | stat.S_IXUSR)
    assert _probe_version(str(silent)) is None
    multi = tmp_path / "multi"
    multi.write_text("#!/bin/sh\necho line1\necho line2\n", encoding="utf-8")
    multi.chmod(multi.stat().st_mode | stat.S_IXUSR)
    assert _probe_version(str(multi)) == "line1"


def test_probe_version_timeout(monkeypatch, tmp_path):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    slow.chmod(slow.stat().st_mode | stat.S_IXUSR)

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["x"], timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise)
    assert _probe_version(str(slow)) is None


def test_registry_get_invalid_toml_returns_none(tmp_path):
    reg = AgentRegistry(tmp_path)
    (reg.dir / "x.toml").write_text("not toml ===", encoding="utf-8")
    assert reg.get("x") is None
