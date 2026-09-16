from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from memvara import Memvara, NullLLM

from .models import Phase


class Maestro:
    """Claude-supervised orchestration with Codex as the implementation agent.

    Claude is the supervisor. Maestro is the coordinator. Codex implements. Memvara is
    the durable shared state layer. Long-running Codex work is delegated to a detached
    worker process so MCP calls return immediately.

    Memvara is the source of truth for task identity/state. tasks.json is only a local
    compatibility cache and can be rebuilt after deletion or drift.
    """

    _REGISTRY_SUBJECT = "maestro:registry"
    _REGISTRY_PREDICATE = "task"

    def __init__(self, root: str | Path = ".") -> None:
        self.root = self._resolve_workspace(root)
        self.state_dir = self.root / ".maestro"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.design_dir = self.state_dir / "designs"
        self.staged_dir = self.state_dir / "staged"
        self.design_dir.mkdir(parents=True, exist_ok=True)
        self.staged_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tasks").mkdir(parents=True, exist_ok=True)
        self.index_path = self.state_dir / "tasks.json"
        self.lock_path = self.state_dir / "task-index.lock"
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

    @contextmanager
    def _task_lock(self) -> Iterator[None]:
        """Serialize numeric task allocation on Unix/macOS while remaining portable."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

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
            if effort not in {"low", "medium", "high", "xhigh", "max"}:
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

    def _registry_record(self, task_id: str, number: int, title: str, created_at: str | None = None) -> dict[str, Any]:
        return {
            "number": int(number),
            "task_id": task_id,
            "title": title,
            "created_at": created_at or self._now().isoformat(),
            "workspace": str(self.root),
        }

    def _registry_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            claims = self.mem.history(self._REGISTRY_SUBJECT, self._REGISTRY_PREDICATE)
        except Exception:
            claims = []
        for claim in claims:
            try:
                value = json.loads(claim.object)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or not value.get("task_id"):
                continue
            try:
                value["number"] = int(value["number"])
            except (TypeError, ValueError, KeyError):
                continue
            records.append(value)
        # A registry record is immutable; keep the newest copy if duplicates exist.
        by_id: dict[str, dict[str, Any]] = {}
        for record in records:
            by_id[str(record["task_id"])] = record
        return sorted(by_id.values(), key=lambda x: int(x["number"]))

    def _write_registry_record(self, record: dict[str, Any]) -> None:
        self.mem.remember(
            self._REGISTRY_SUBJECT,
            self._REGISTRY_PREDICATE,
            json.dumps(record, sort_keys=True),
            valid_from=self._now(),
            recorded_at=self._now(),
        )

    def _load_index(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_index(self, items: list[dict[str, Any]]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    def _migrate_legacy_index(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Import v0.4 tasks.json records into the durable Memvara registry once."""
        registry = self._registry_records()
        known = {str(x["task_id"]) for x in registry}
        migrated = list(registry)
        for item in items:
            task_id = str(item.get("task_id", ""))
            if not task_id or task_id in known:
                continue
            try:
                number = int(item["number"])
            except (TypeError, ValueError, KeyError):
                continue
            record = self._registry_record(
                task_id,
                number,
                str(item.get("title") or task_id),
                str(item.get("created_at") or self._now().isoformat()),
            )
            self._write_registry_record(record)
            known.add(task_id)
            migrated.append(record)
        if migrated:
            migrated.sort(key=lambda x: int(x["number"]))
            self._save_index(migrated)
        return migrated

    def _recover_from_live_claims(self) -> list[dict[str, Any]]:
        """Recover v0.4 tasks even when both tasks.json and registry are absent."""
        subjects: dict[str, dict[str, Any]] = {}
        try:
            claims = self.mem.get_all()
        except Exception:
            claims = []
        for claim in claims:
            subject = getattr(claim, "subject", None)
            predicate = getattr(claim, "predicate", None)
            if not isinstance(subject, str) or not subject.startswith("maestro:task:"):
                continue
            task_id = subject.removeprefix("maestro:task:")
            if not task_id:
                continue
            entry = subjects.setdefault(task_id, {})
            value = getattr(claim, "object", None)
            if predicate == "task_number" and value is not None:
                try:
                    entry["number"] = int(value)
                except (TypeError, ValueError):
                    pass
            elif predicate == "task_title" and value is not None:
                entry["title"] = str(value)
            elif predicate == "task_workspace" and value is not None:
                entry["workspace"] = str(value)
            elif predicate == "task_design" and value is not None:
                entry["design"] = str(value)
        recovered: list[dict[str, Any]] = []
        max_number = max((int(v.get("number", 0)) for v in subjects.values()), default=0)
        for task_id, data in sorted(subjects.items()):
            number = data.get("number")
            if number is None:
                max_number += 1
                number = max_number
            record = self._registry_record(
                task_id,
                int(number),
                str(data.get("title") or task_id),
            )
            if data.get("workspace"):
                record["workspace"] = data["workspace"]
            self._write_registry_record(record)
            recovered.append(record)
        recovered.sort(key=lambda x: int(x["number"]))
        if recovered:
            self._save_index(recovered)
        return recovered

    def _index_items(self) -> list[dict[str, Any]]:
        """Return the task index, rebuilding it from Memvara when necessary."""
        registry = self._registry_records()
        if registry:
            self._save_index(registry)
            return registry
        legacy = self._load_index()
        if legacy:
            return self._migrate_legacy_index(legacy)
        return self._recover_from_live_claims()

    def _new_task_number(self) -> int:
        items = self._index_items()
        return max((int(x["number"]) for x in items), default=0) + 1

    def _register_task(self, task_id: str, title: str, number: int | None = None) -> int:
        if number is None:
            number = self._new_task_number()
        record = self._registry_record(task_id, number, title)
        self._write_registry_record(record)
        items = [x for x in self._index_items() if x["task_id"] != task_id]
        items.append(record)
        items.sort(key=lambda x: int(x["number"]))
        self._save_index(items)
        return number

    def resolve_task(self, ref: str) -> str:
        ref = str(ref).strip()
        items = self._index_items()
        if ref.isdigit():
            number = int(ref)
            for item in items:
                if int(item["number"]) == number:
                    return str(item["task_id"])
            available = ", ".join(str(x["number"]) for x in items) or "none"
            raise KeyError(f"Unknown task number {ref}. Available task numbers: {available}")
        if ref.startswith("task-"):
            if any(str(item["task_id"]) == ref for item in items):
                return ref
            try:
                claims = self.mem.history(self._subject(ref), "task_status")
            except Exception:
                claims = []
            if claims:
                return ref
        raise KeyError(f"Unknown task reference {ref!r}. Run `maestro list` to see available tasks.")

    def list_tasks(self) -> list[dict[str, Any]]:
        items = self._index_items()
        output: list[dict[str, Any]] = []
        for item in items:
            try:
                state = self.status(item["task_id"])
            except KeyError:
                continue
            output.append({**item, **state})
        return sorted(output, key=lambda x: int(x["number"]))

    def _stage_path(self, handoff_file: str | Path) -> Path:
        path = Path(handoff_file).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        try:
            path.relative_to(self.staged_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"Handoff file must be under {self.staged_dir}") from exc
        return path

    def _load_staged_handoff(self, handoff_file: str | Path) -> tuple[Path, dict[str, Any]]:
        path = self._stage_path(handoff_file)
        if not path.exists():
            raise ValueError(f"Handoff file does not exist: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid handoff file: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Handoff file must contain a JSON object")
        for key in ("title", "request", "design_file"):
            if not payload.get(key):
                raise ValueError(f"Handoff file is missing required field: {key}")
        design_path = Path(str(payload["design_file"])).expanduser()
        if not design_path.is_absolute():
            design_path = self.root / design_path
        design_path = design_path.resolve()
        try:
            design_path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("design_file must be inside the active workspace") from exc
        if not design_path.is_file():
            raise ValueError(f"Design file does not exist: {design_path}")
        payload["design_file"] = str(design_path)
        return path, payload

    def create_handoff_from_file(self, handoff_file: str | Path) -> dict[str, Any]:
        """Create/reuse a handoff from a tiny staged metadata file.

        The staged file deliberately keeps the large design out of the MCP call. It
        remains on disk until task creation *and* worker launch succeed, so a transient
        MCP/subprocess failure can retry the exact same handoff without regenerating the
        design or paying the context cost again.
        """
        stage_path, payload = self._load_staged_handoff(handoff_file)
        existing_task_id = payload.get("task_id")
        if existing_task_id:
            task_id = self.resolve_task(str(existing_task_id))
            status = self.status(task_id)
            status["staged_handoff"] = str(stage_path)
            return status

        design = Path(payload["design_file"]).read_text(encoding="utf-8")
        task = self.create_handoff(
            str(payload["title"]),
            str(payload["request"]),
            design,
            model=payload.get("model"),
            effort=payload.get("effort"),
        )
        payload["task_id"] = task["task_id"]
        payload["task_number"] = task["task_number"]
        payload["created_at"] = self._now().isoformat()
        tmp = stage_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(stage_path)
        return {**task, "staged_handoff": str(stage_path)}

    def create_handoff(self, title: str, request: str, design: str, model: str | None = None, effort: str | None = None) -> dict[str, Any]:
        selected_model = model or self.config.get("model")
        selected_effort = effort or self.config.get("effort")
        if selected_effort is not None:
            selected_effort = str(selected_effort).lower()
            if selected_effort not in {"low", "medium", "high", "xhigh", "max"}:
                raise ValueError(f"Unsupported Codex reasoning effort: {selected_effort}")

        task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        path = self.design_dir / f"{task_id}.md"
        path.write_text(design, encoding="utf-8")

        with self._task_lock():
            task_number = self._new_task_number()
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
            self._write_claim(task_id, "task_title", title, episode.episode_ids)
            if selected_model:
                self._write_claim(task_id, "task_model", selected_model, episode.episode_ids)
            if selected_effort:
                self._write_claim(task_id, "task_effort", selected_effort, episode.episode_ids)
            self._register_task(task_id, title, task_number)

        return {
            "task_id": task_id,
            "task_number": task_number,
            "design_path": str(path),
            "phase": Phase.DESIGNED.value,
            "model": selected_model,
            "effort": selected_effort,
        }

    def _worker_command(self, task_id: str, action: str, review: str | None = None) -> list[str]:
        cmd = [sys.executable, "-m", "maestro.worker", action, task_id, "--workspace", str(self.root)]
        if review is not None:
            cmd.extend(["--review", review])
        return cmd

    def finalize_staged_handoff(self, handoff_file: str | Path, task_id: str) -> str:
        stage_path = self._stage_path(handoff_file)
        if not stage_path.exists():
            return str(stage_path)
        task_dir = self.state_dir / "tasks" / self.resolve_task(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        destination = task_dir / "handoff.json"
        stage_path.replace(destination)
        return str(destination)

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
        items = self._index_items()
        index = next((x for x in items if str(x["task_id"]) == task_id), None)
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
