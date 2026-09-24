"""Pins for the docs/usage Diátaxis section: structure, type discipline, and
stable identifiers so the reference docs cannot drift from the product surface."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USAGE = ROOT / "docs" / "usage"

EXPECTED_FILES = [
    "README.md",
    "tutorials/first-delegation.md",
    "how-to/register-an-agent.md",
    "how-to/delegate-a-task.md",
    "how-to/inject-context.md",
    "how-to/manage-in-flight-tasks.md",
    "how-to/inspect-tasks-and-artifacts.md",
    "how-to/run-cross-machine.md",
    "reference/cli.md",
    "reference/mcp-tools.md",
    "reference/handoff-format.md",
    "reference/configuration.md",
    "explanation/how-delegation-works.md",
]

MCP_TOOLS = (
    "delegate",
    "task_wait",
    "task_status",
    "list_tasks",
    "agents_list",
    "cancel_task",
    "answer_task_question",
    "followup",
    "rename_task_branch",
    "cleanup_task_worktree",
)

CLI_COMMANDS = (
    "maestro-daemon",
    "delegate",
    "task list",
    "task status",
    "task tail",
    "task audit",
    "task continue",
    "task answer",
    "task cancel",
    "task rename-branch",
    "task cleanup",
    "dashboard",
    "agents add",
    "agents discover",
    "peers add",
    "budgets",
    "config",
    "storage migrate-memvara",
    "gc",
)

HANDOFF_FIELDS = (
    "title",
    "request",
    "design",
    "context_files",
    "context",
    "target_agent",
    "fallback",
    "origin_agent",
    "artifacts",
    "verification",
    "verification_command",
    "commit_policy",
    "branch",
    "budget_hint",
    "sensitive",
    "max_depth_remaining",
    "agent_settings",
)

ENV_VARS = (
    "MAESTRO_HOME",
    "MAESTRO_WORKSPACE",
    "MAESTRO_DAEMON_URL",
    "MAESTRO_DAEMON_TOKEN",
    "MAESTRO_MAX_RETRIES",
    "MAESTRO_BACKOFF_S",
    "MAESTRO_DELEGATE_TIMEOUT",
    "MAESTRO_BUDGET_PER_AGENT_USD",
    "MAESTRO_BUDGET_DAILY_USD",
    "MAESTRO_DISCOVERY",
    "MAESTRO_NODE_NAME",
    "MAESTRO_STORAGE",
    "MAESTRO_CONTINUATION_MAX_TOKENS",
)


def _read(rel):
    return (USAGE / rel).read_text(encoding="utf-8")


def test_usage_docs_tree_exists_and_is_substantial():
    for rel in EXPECTED_FILES:
        path = USAGE / rel
        assert path.is_file(), f"missing docs file: {rel}"
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines >= 30, f"{rel} is suspiciously short ({lines} lines)"


def test_index_links_resolve():
    text = _read("README.md")
    links = re.findall(r"\]\(([^)#]+\.md)\)", text)
    assert links, "index should link to the section documents"
    for link in set(links):
        if link.startswith("../"):  # outside this section (docs/ or repo root)
            target = (USAGE / link).resolve()
        else:
            target = (USAGE / link)
        assert target.is_file(), f"dangling index link: {link}"


def test_tutorial_is_a_single_line_lesson():
    text = _read("tutorials/first-delegation.md")
    assert "In this tutorial we will" in text
    # Tutorials narrate expected output instead of explaining mechanisms.
    assert "You will see" in text or "You will notice" in text
    assert "maestro delegate" in text and "maestro-daemon" in text


def test_how_to_guides_open_with_their_goal():
    for rel in EXPECTED_FILES:
        if not rel.startswith("how-to/"):
            continue
        text = _read(rel)
        assert "This guide shows you how to" in text, f"{rel} missing goal opener"


def test_mcp_reference_pins_all_tools_and_states():
    text = _read("reference/mcp-tools.md")
    for tool in MCP_TOOLS:
        assert tool in text, f"MCP reference missing tool {tool}"
    for state in ("submitted", "working", "input-required", "completed", "failed", "canceled"):
        assert state in text, f"MCP reference missing state {state}"


def test_cli_reference_pins_all_commands():
    text = _read("reference/cli.md")
    for command in CLI_COMMANDS:
        assert command in text, f"CLI reference missing command {command!r}"
    # Exit codes are part of the contract.
    assert "130" in text


def test_handoff_reference_pins_fields_and_legacy_mapping():
    text = _read("reference/handoff-format.md")
    for section in ("handoff", "routing", "expectations", "constraints"):
        assert f"[{section}]" in text, f"handoff reference missing section [{section}]"
    for field in HANDOFF_FIELDS:
        assert field in text, f"handoff reference missing field {field}"
    # Legacy 0.8.x mapping stays documented (data compatibility).
    for legacy in ("design_file", "implementer", "supervisor"):
        assert legacy in text, f"handoff reference missing legacy field {legacy}"


def test_configuration_reference_pins_env_vars_and_layout():
    text = _read("reference/configuration.md")
    for var in ENV_VARS:
        assert var in text, f"configuration reference missing {var}"
    for artifact in ("registry.json", "state.jsonl", "daemon.json", "peers.json"):
        assert artifact in text, f"configuration reference missing {artifact}"


def test_explanation_is_about_not_how_to():
    text = _read("explanation/how-delegation-works.md")
    # Explanation frames the subject ("why/why not"), never as instructions.
    assert "Why a daemon at all" in text
    assert "This is an explanation of" in text
