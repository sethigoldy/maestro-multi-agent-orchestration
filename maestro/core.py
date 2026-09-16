from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memvara import Memvara, NullLLM

from .models import Phase


class Maestro:
    """Claude-supervised orchestration with Codex as the implementation agent.

    Claude is the supervisor. Maestro is the coordinator. Codex implements. Memvara is
    the durable shared state layer. Long-running Codex work is delegated to a detached
    worker process so MCP calls return immediately.
    """

    def __init__(self, root: str | Path = ".") -> None:
        self.root = self._resolve_workspace(root)
        self.state_dir = self.root / ".maestro"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.design_dir = self.state_dir / "designs"
        self.design_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tasks").mkdir(parents=True, exist_ok=True)
        self.index_path = self.state_dir / "tasks.json"
        self.config = self._load_config()
        self.mem = Memvara(
            str(self.state_dir / "memory.db"),
            user=os.environ.get("MEMVARA_USER", os.environ.get("USER", "local")),
            tenant=os.environ.get("MEMVARA_TENANT", "default"),
            llm=NullLLM(),
        )

    @staticmethod
    def _resolve_workspace(root: str | Path) -> Path:
        workspace = Path(root).expanduser().resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {workspace}")
        return workspace

    @staticmethod
    def git_root(workspace: str | Path) -> Path:
        result = subprocess.run(
            ["git", "-C", str(Path(workspace).resolve()), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"Workspace is not a Git repository: {workspace}")
        return Path(result.stdout.strip()).resolve()

    def _load_config(self) -> dict[str, Any]:
        path = self.state_dir / "config.toml"
        config: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open("rb") as fh:
                    raw = tomllib.load(fh)
                config = raw.get("codex", {}) if isinstance(raw, dict) else {}
            except (OSError, tomllib.TOMLDecodeError):
                config = {}
        model = config.get("model") or os.environ.get("MAESTRO_CODEX_MODEL")
        effort = config.get("effort") or os.environ.get("MAESTRO_CODEX_EFFORT")
        if effort is not None:
            effort = str(effort).lower()
            if effort not in {"low", "medium", "high", "xhigh"}:
                raise ValueError(f"Unsupported Codex reasoning effort: {effort}")
        return {"model": str(model) if model else None, "effort": effort}

    def codex_defaults(self) -> dict[str, Any]:
        return dict(self.config)

    def close(self) -> None:
        self.mem.close()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _subject(self, task_id: str) -> str:
        return f"maestro:task:{task_id}"

    def _write_claim(self, task_id: str, predicate: str, value: str, sources=None) -> None:
        now = self._now()
        self.mem.remember(
            self._subject(task_id), predicate, value,
            sources=sources or [], valid_from=now, recorded_at=now,
        )

    def _load_index(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _save_index(self, items: list[dict[str, Any]]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    def _new_task_number(self) -> int:
        items = self._load_index()
        return max((int(x["number"]) for x in items), default=0) + 1

    def _register_task(self, task_id: str, title: str) -> int:
        number = self._new_task_number()
        items = self._load_index()
        items.append({"number": number, "task_id": task_id, "title": title,
                      "created_at": self._now().isoformat()})
        self._save_index(items)
        return number

    def resolve_task(self, ref: str) -> str:
        ref = str(ref).strip()
        items = self._load_index()
        if ref.isdigit():
            number = int(ref)
            for item in items:
                if int(item["number"]) == number:
                    return item["task_id"]
            raise KeyError(ref)
        return ref

    def list_tasks(self) -> list[dict[str, Any]]:
        items = self._load_index()
        output: list[dict[str, Any]] = []
        for item in items:
            try:
                state = self.status(item["task_id"])
            except KeyError:
                continue
            output.append({**item, **state})
        return sorted(output, key=lambda x: int(x["number"]))

    def create_handoff(self, title: str, request: str, design: str, model: str | None = None, effort: str | None = None) -> dict[str, Any]:
        task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        task_number = self._register_task(task_id, title)
        selected_model = model or self.config.get("model")
        selected_effort = (effort or self.config.get("effort"))
        if selected_effort is not None:
            selected_effort = str(selected_effort).lower()
            if selected_effort not in {"low", "medium", "high", "xhigh"}:
                raise ValueError(f"Unsupported Codex reasoning effort: {selected_effort}")
        path = self.design_dir / f"{task_id}.md"
        path.write_text(design, encoding="utf-8")
        episode = self.mem.add(
            f"Approved design handoff. Task: {task_id}. Task number: {task_number}. "
            f"Title: {title}. Request: {request}\n\n{design}",
            role="system", ts=self._now(),
        )
        self._write_claim(task_id, "task_status", Phase.DESIGNED.value, episode.episode_ids)
        self._write_claim(task_id, "task_owner", "claude", episode.episode_ids)
        self._write_claim(task_id, "task_implementer", "codex", episode.episode_ids)
        self._write_claim(task_id, "task_design", str(path), episode.episode_ids)
        self._write_claim(task_id, "task_workspace", str(self.root), episode.episode_ids)
        self._write_claim(task_id, "task_number", str(task_number), episode.episode_ids)
        if selected_model:
            self._write_claim(task_id, "task_model", selected_model, episode.episode_ids)
        if selected_effort:
            self._write_claim(task_id, "task_effort", selected_effort, episode.episode_ids)
        return {"task_id": task_id, "task_number": task_number,
                "design_path": str(path), "phase": Phase.DESIGNED.value,
                "model": selected_model, "effort": selected_effort}

    def _worker_command(self, task_id: str, action: str, review: str | None = None) -> list[str]:
        cmd = [sys.executable, "-m", "maestro.worker", action, task_id, "--workspace", str(self.root)]
        if review is not None:
            cmd.extend(["--review", review])
        return cmd

    def launch(self, task_id: str, action: str, review: str | None = None) -> dict[str, Any]:
        task_id = self.resolve_task(task_id)
        task_dir = self.state_dir / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        log_path = task_dir / f"{action}.log"
        log = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            self._worker_command(task_id, action, review),
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
        (task_dir / f"{action}.pid").write_text(str(process.pid), encoding="utf-8")
        log.close()
        return {"task_id": task_id, "pid": process.pid, "action": action,
                "log": str(log_path)}

    def implement_async(self, task_id: str) -> dict[str, Any]:
        task_id = self.resolve_task(task_id)
        self._write_claim(task_id, "task_status", Phase.IMPLEMENTING.value)
        return self.launch(task_id, "implement")

    def fix_async(self, task_id: str, review: str) -> dict[str, Any]:
        task_id = self.resolve_task(task_id)
        self._write_claim(task_id, "task_status", Phase.FIXING.value)
        return self.launch(task_id, "fix", review)

    def status(self, task_ref: str) -> dict[str, Any]:
        task_id = self.resolve_task(task_ref)
        result: dict[str, Any] = {"task_id": task_id}
        index = next((x for x in self._load_index() if x["task_id"] == task_id), None)
        result["task_number"] = index["number"] if index else None
        result["title"] = index["title"] if index else None
        for predicate, key in [
            ("task_status", "phase"),
            ("task_owner", "supervisor"),
            ("task_implementer", "implementer"),
            ("task_design", "design"),
            ("task_result", "result"),
            ("task_verification", "verification"),
            ("task_workspace", "workspace"),
            ("task_model", "model"),
            ("task_effort", "effort"),
        ]:
            claims = self.mem.history(self._subject(task_id), predicate)
            result[key] = claims[-1].object if claims else None
        return result

    def review(self, task_id: str, review: str, approved: bool) -> dict[str, object]:
        task_id = self.resolve_task(task_id)
        phase = Phase.COMPLETE if approved else Phase.FIXING
        episode = self.mem.add(
            f"Claude review for {task_id}. Approved={approved}.\n{review}",
            role="system", ts=self._now(),
        )
        self._write_claim(task_id, "task_review", review, episode.episode_ids)
        self._write_claim(task_id, "task_status", phase.value, episode.episode_ids)
        return {"task_id": task_id, "approved": approved, "phase": phase.value}
