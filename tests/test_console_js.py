"""Behavioral checks of the console's pure JS logic (skipped without node)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_JS = REPO_ROOT / "web" / "src" / "lib" / "events.js"


def _node() -> str | None:
    return shutil.which("node")


def test_normalize_task_maps_a2a_shape():
    node = _node()
    if node is None:
        import pytest

        pytest.skip("node not available; JS normalizer not tested")
    script = f"""
import {{ normalizeTask }} from {json.dumps(str(EVENTS_JS))};
const record = {{
  kind: "task",
  id: "task-1",
  status: {{ state: "completed" }},
  metadata: {{
    title: "T", workspace: "/w", branch: "maestro/task-1",
    origin_agent: "human", target_agent: "codex",
    usage: {{ cost_usd: 0.5 }}, attempts: [{{ agent: "codex" }}], error: null,
  }},
}};
const flat = normalizeTask(record);
const assert = (cond, msg) => {{ if (!cond) throw new Error(msg); }};
assert(flat.task_id === "task-1", "task_id");
assert(flat.state === "completed", "state");
assert(flat.title === "T", "title");
assert(flat.usage.cost_usd === 0.5, "usage");
assert(flat.attempts.length === 1, "attempts");
const bare = normalizeTask({{ id: "x" }});
assert(bare.state === "unknown" && Array.isArray(bare.attempts), "bare record");
console.log("ok");
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr


def test_bundle_has_no_polling():
    bundle = REPO_ROOT / "maestro" / "web_dist" / "console.js"
    assert bundle.is_file(), "run `node web/build.mjs` to produce maestro/web_dist/"
    js = bundle.read_text(encoding="utf-8")
    assert "setInterval" not in js
    assert "EventSource" in js
