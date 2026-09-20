from __future__ import annotations

import pytest

from maestro.handoff import HandoffDoc
from maestro.modes import DEFAULT_MAX_BOUNCES, ModePreset, expand, parse_modes, resolve_mode


def _doc(**overrides) -> HandoffDoc:
    base = dict(title="Do it", request="Implement it")
    base.update(overrides)
    return HandoffDoc(**base)


# ------------------------------------------------------------ parse / validate
def test_parse_modes_absent_returns_empty():
    assert parse_modes(None) == {}
    assert parse_modes({}) == {}


def test_parse_modes_full_preset():
    modes = parse_modes({"economy": {"implementer": "a", "verifier": "v", "reviewer": "r", "fixer": "f", "max_bounces": 3}})
    preset = modes["economy"]
    assert (preset.implementer, preset.verifier, preset.reviewer, preset.fixer, preset.max_bounces) == ("a", "v", "r", "f", 3)
    assert preset.name == "economy"


def test_parse_modes_defaults_for_unset_slots():
    preset = parse_modes({"m": {"implementer": "a"}})["m"]
    assert (preset.verifier, preset.reviewer, preset.fixer, preset.max_bounces) == (None, None, None, DEFAULT_MAX_BOUNCES)


@pytest.mark.parametrize(
    ("table", "needle"),
    [
        ({}, "requires an 'implementer'"),
        ({"implementer": 5}, "non-empty agent name"),
        ({"implementer": ""}, "non-empty agent name"),
        ({"implementer": "a", "bogus": 1}, "unknown keys"),
        ({"implementer": "a", "verifier": 7}, "non-empty agent name"),
        ({"implementer": "a", "reviewer": " "}, "non-empty agent name"),
        ({"implementer": "a", "fixer": None, "max_bounces": -1}, "integer >= 0"),
        ({"implementer": "a", "max_bounces": True}, "integer >= 0"),
        ({"implementer": "a", "max_bounces": 1.5}, "integer >= 0"),
    ],
)
def test_parse_modes_rejects_malformed(table, needle):
    with pytest.raises(ValueError, match=needle):
        parse_modes({"bad": table})


def test_parse_modes_non_table_raises():
    with pytest.raises(ValueError, match="must be a table of preset tables"):
        parse_modes(["nope"])
    with pytest.raises(ValueError, match=r"\[modes\.x\] must be a table"):
        parse_modes({"x": "nope"})


def test_preset_to_dict_is_json_safe():
    preset = ModePreset(name="m", implementer="a", verifier=None, reviewer="r", fixer=None, max_bounces=1)
    assert preset.to_dict() == {"implementer": "a", "verifier": None, "reviewer": "r", "fixer": None, "max_bounces": 1}


# --------------------------------------------------------------------- resolve
def test_resolve_mode_returns_preset():
    modes = parse_modes({"alpha": {"implementer": "a"}})
    assert resolve_mode(modes, "alpha").implementer == "a"


def test_resolve_mode_unknown_lists_available():
    modes = parse_modes({"alpha": {"implementer": "a"}, "beta": {"implementer": "b"}})
    with pytest.raises(ValueError, match=r"Unknown work mode 'zzz'. Defined modes: alpha, beta"):
        resolve_mode(modes, "zzz")
    with pytest.raises(ValueError, match=r"Defined modes: none"):
        resolve_mode({}, "zzz")


# ----------------------------------------------------------------------- expand
def test_expand_fills_unset_slots():
    preset = ModePreset(name="m", implementer="impl", verifier="ver", reviewer="rev", fixer="fix", max_bounces=5)
    doc = expand(preset, _doc())
    assert (doc.target_agent, doc.verify_agent, doc.review_agent, doc.fix_agent, doc.max_bounces) == ("impl", "ver", "rev", "fix", 5)


def test_expand_explicit_fields_win_over_preset():
    preset = ModePreset(name="m", implementer="impl", verifier="ver", reviewer="rev", fixer="fix", max_bounces=5)
    doc = expand(
        preset,
        _doc(target_agent="mine", explicit_target=True, review_agent="myreview", verify_agent="myverify", fix_agent="myfix", max_bounces=9),
    )
    assert (doc.target_agent, doc.verify_agent, doc.review_agent, doc.fix_agent, doc.max_bounces) == ("mine", "myverify", "myreview", "myfix", 9)


def test_expand_fixer_defaults_to_implementer():
    preset = ModePreset(name="m", implementer="impl")
    doc = expand(preset, _doc())
    assert doc.fix_agent == "impl" and doc.max_bounces == DEFAULT_MAX_BOUNCES
    assert doc.verify_agent is None and doc.review_agent is None


# ------------------------------------------------------- wire round-trip (CLI)
def test_mode_preset_survives_cli_wire_round_trip():
    """`maestro delegate --mode m` (no --target) must pin the preset implementer.

    The CLI defaults target_agent to "codex" but sets explicit_target=False;
    that flag must cross the JSON-RPC wire (to_dict/from_dict), otherwise the
    legacy heuristic in from_dict treats the default as user-explicit and the
    preset never replaces it (regression: demo ran the real codex, not the fake).
    """
    from maestro.handoff import from_dict

    preset = ModePreset(name="demo", implementer="fake-impl", verifier="fake-ver", reviewer=None, fixer=None, max_bounces=1)
    doc = _doc(target_agent="codex", mode="demo", explicit_target=False)  # exactly what the CLI sends
    wire = from_dict(doc.to_dict())
    assert wire.explicit_target is False, "explicit_target must survive serialization"
    expanded = expand(preset, wire)
    assert (expanded.target_agent, expanded.verify_agent, expanded.fix_agent) == ("fake-impl", "fake-ver", "fake-impl")


def test_explicit_target_survives_wire_round_trip():
    from maestro.handoff import from_dict

    preset = ModePreset(name="demo", implementer="fake-impl")
    doc = _doc(target_agent="mine", mode="demo", explicit_target=True)
    wire = from_dict(doc.to_dict())
    assert wire.explicit_target is True
    expanded = expand(preset, wire)
    assert expanded.target_agent == "mine"  # user's explicit target wins over the preset


def test_from_dict_legacy_record_without_flag_keeps_old_heuristic():
    """Records persisted before explicit_target was serialized still parse."""
    from maestro.handoff import from_dict

    data = {
        "handoff": {"title": "t", "request": "r"},
        "routing": {"target_agent": "codex", "mode": None},
        "expectations": {},
        "constraints": {},
    }
    assert from_dict(data).explicit_target is True  # legacy: named target counts as explicit
