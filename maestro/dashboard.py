"""Web console assets served by the daemon at ``GET /`` and ``GET /console.js``.

The console is a small React app (source in ``web/src``, built with esbuild by
``web/build.mjs`` into ``maestro/web_dist/``). The built artifacts are committed,
so serving them needs no Node toolchain at runtime. It is strictly reactive:
the initial task list comes from ``GET /tasks`` (one fetch), and everything
after that flows over the global SSE stream ``GET /events`` — no polling.
"""

from __future__ import annotations

import json
from pathlib import Path

WEB_DIST = Path(__file__).resolve().parent / "web_dist"

# path suffix -> (filename, content type)
CONSOLE_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
}


def console_asset(path: str) -> tuple[str, bytes] | None:
    """Return (content_type, body) for a known console asset, else None."""
    entry = CONSOLE_ASSETS.get(path)
    if entry is None:
        return None
    filename, content_type = entry
    try:
        body = (WEB_DIST / filename).read_bytes()
    except OSError:
        return None
    return content_type, body


def console_manifest() -> dict[str, str]:
    """Small JSON summary of the served console (for /tasks-style debugging)."""
    out: dict[str, str] = {}
    for path in sorted(CONSOLE_ASSETS):
        entry = CONSOLE_ASSETS[path]
        try:
            size = (WEB_DIST / entry[0]).stat().st_size
        except OSError:
            size = 0
        out[path] = f"{entry[0]} ({size} bytes)"
    return json.loads(json.dumps(out))
