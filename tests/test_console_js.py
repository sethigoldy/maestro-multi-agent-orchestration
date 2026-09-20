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
    # token support for non-loopback daemons must be compiled into the bundle
    assert "maestro_token" in js


def test_web_dist_is_in_sync_with_source():
    """Catch a stale maestro/web_dist/ after web/src edits — no node required.

    The build copies web/index.html verbatim and bundles string literals that
    survive minification, so these markers prove the checked-in assets were
    built from the current sources. (CI additionally rebuilds with the pinned
    esbuild and diffs the output; see .github/workflows/test.yml.)
    """
    src_index = REPO_ROOT / "web" / "index.html"
    dist_index = REPO_ROOT / "maestro" / "web_dist" / "index.html"
    assert src_index.is_file() and dist_index.is_file(), (
        "run `node web/build.mjs` to produce maestro/web_dist/"
    )
    assert dist_index.read_bytes() == src_index.read_bytes(), (
        "maestro/web_dist/index.html is stale relative to web/index.html; "
        "run `node web/build.mjs`"
    )
    bundle = REPO_ROOT / "maestro" / "web_dist" / "console.js"
    assert bundle.is_file() and bundle.stat().st_size > 0
    js = bundle.read_text(encoding="utf-8")
    # Each marker comes from the current web/src (DetailPane receipt panel,
    # events.loadReceipt). If one is missing, the bundle predates a source edit.
    for marker in ("VERIFIED", "Final verification: ", "Final: ", "/receipt"):
        assert marker in js, (
            f"stale bundle: {marker!r} (from web/src) not found in "
            "maestro/web_dist/console.js; run `node web/build.mjs`"
        )


def test_auth_module_token_flow():
    node = _node()
    if node is None:
        import pytest

        pytest.skip("node not available; JS auth module not tested")
    auth_js = REPO_ROOT / "web" / "src" / "lib" / "auth.js"
    script = f"""
import {{ initToken, authHeaders, withToken }} from {json.dumps(str(auth_js))};
// minimal DOM stand-ins
globalThis.sessionStorage = new Map();
sessionStorage.getItem = (k) => (sessionStorage.has(k) ? sessionStorage.get(k) : null);
sessionStorage.setItem = (k, v) => sessionStorage.set(k, v);
let replaced = null;
globalThis.window = {{
  location: {{ href: "http://10.0.0.5:8790/?token=abc%20def" }},
  history: {{ replaceState: (state, title, url) => {{ replaced = url; }} }},
}};
const assert = (cond, msg) => {{ if (!cond) throw new Error(msg); }};
// no token yet -> headers empty, paths untouched
assert(JSON.stringify(authHeaders()) === "{{}}", "no token headers");
assert(withToken("/tasks") === "/tasks", "no token path");
const tok = initToken();
assert(tok === "abc def", "initToken decodes ?token= got " + tok);
assert(replaced === "http://10.0.0.5:8790/", "address bar stripped, got " + replaced);
// token now flows into headers and EventSource URLs
const h = authHeaders();
assert(h["Authorization"] === "Bearer abc def", "bearer header");
assert(withToken("/events") === "/events?token=" + encodeURIComponent("abc def"), "eventsource url");
console.log("ok");
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr
