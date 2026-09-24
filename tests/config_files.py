"""Write Maestro config files for tests.

The daemon reads its config from disk for each task (the user file, then the
project's .maestro/config.toml, then the workspace's), so a test sets config
the way a user does: by writing the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {value!r} to TOML")


def _table(prefix: str, body: dict[str, Any], lines: list[str]) -> None:
    scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in body.items() if isinstance(v, dict)}
    if scalars or not tables:
        lines.append(f"[{prefix}]")
        lines.extend(f"{key} = {_value(value)}" for key, value in scalars.items())
    for key, value in tables.items():
        _table(f"{prefix}.{key}", value, lines)


def write_config(directory: str | Path, sections: dict[str, dict[str, Any]], *, project: bool = False) -> Path:
    """Write ``sections`` as TOML tables, replacing the file.

    ``directory`` is the state directory (the user config), or with
    ``project=True`` a workspace, whose .maestro/config.toml is written."""
    base = Path(directory) / ".maestro" if project else Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, body in sections.items():
        _table(name, body, lines)
    path = base / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
