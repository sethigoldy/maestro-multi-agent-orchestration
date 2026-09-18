from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def maestro_user_dir() -> Path:
    """Return Maestro's user-level state directory."""
    override = os.environ.get("MAESTRO_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Maestro"
    return Path.home() / ".maestro"


@dataclass
class _Claim:
    subject: str
    predicate: str
    object: str
    episode_ids: list[str] | None = None


@dataclass
class _Episode:
    episode_ids: list[str]


class _FileState:
    """Small append-only claim store at user scope."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "state.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _read(self) -> list[_Claim]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[_Claim] = []
        for line in lines:
            try:
                value = json.loads(line)
                out.append(_Claim(str(value["subject"]), str(value["predicate"]), str(value["object"]), list(value.get("episode_ids") or [])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return out

    def _append(self, claim: _Claim) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"subject": claim.subject, "predicate": claim.predicate, "object": claim.object, "episode_ids": claim.episode_ids or []}, ensure_ascii=False) + "\n")
            fh.flush()

    def remember(self, subject: str, predicate: str, object: str, **_: Any) -> None:
        self._append(_Claim(subject, predicate, str(object), [uuid.uuid4().hex]))

    def history(self, subject: str, predicate: str) -> list[_Claim]:
        return [c for c in self._read() if c.subject == subject and c.predicate == predicate]

    def get_all(self) -> list[_Claim]:
        return self._read()

    def forget(self, subject: str) -> int:
        """Rewrite the journal without any claim for ``subject``; returns how many were dropped."""
        lines = self._read()
        kept = [c for c in lines if c.subject != subject]
        dropped = len(lines) - len(kept)
        if dropped:
            tmp = self.path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for claim in kept:
                    fh.write(json.dumps({"subject": claim.subject, "predicate": claim.predicate, "object": claim.object, "episode_ids": claim.episode_ids or []}, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        return dropped

    def add(self, content: str, role: str = "system", ts: datetime | None = None) -> _Episode:
        episode = uuid.uuid4().hex
        self._append(_Claim("maestro:event", role, content, [episode]))
        return _Episode([episode])

    def close(self) -> None:
        return None


class _MemvaraState:
    """Optional Memvara adapter using the same user-level directory."""

    def __init__(self, state_dir: Path, db_name: str = "memory.db") -> None:
        try:
            from memvara import Memvara, NullLLM
        except ImportError as exc:
            raise RuntimeError("Memvara backend requested but the 'memvara' package is not installed. Install Maestro with the memvara extra.") from exc
        self.client = Memvara(str(state_dir / db_name), user=os.environ.get("MEMVARA_USER", os.environ.get("USER", "local")), tenant=os.environ.get("MEMVARA_TENANT", "default"), llm=NullLLM())

    def remember(self, subject: str, predicate: str, object: str, **kwargs: Any) -> None:
        self.client.remember(subject, predicate, object, **kwargs)

    def history(self, subject: str, predicate: str) -> list[Any]:
        return list(self.client.history(subject, predicate))

    def get_all(self) -> list[Any]:
        return list(self.client.get_all())

    def add(self, content: str, role: str = "system", ts: datetime | None = None) -> Any:
        return self.client.add(content, role=role, ts=ts)

    def close(self) -> None:
        self.client.close()


class Maestro:
    _REGISTRY_SUBJECT = "maestro:registry"
    _REGISTRY_PREDICATE = "task"

    def __init__(self, root: str | Path = ".") -> None:
        self.root = self._resolve_workspace(root)

        self.user_state_dir = maestro_user_dir()
        self.user_state_dir.mkdir(parents=True, exist_ok=True)

        (self.user_state_dir / "migrations").mkdir(exist_ok=True)
        (self.user_state_dir / "tasks").mkdir(exist_ok=True)

        self.workspace_state = self.user_state_dir
        self.state_dir = self.user_state_dir

        (self.user_state_dir / "tasks").mkdir(exist_ok=True)

        self.index_path = self.user_state_dir / "registry.json"
        self.lock_path = self.user_state_dir / "registry.lock"

        # self.user_state_dir = maestro_user_dir()
        # self.user_state_dir.mkdir(parents=True, exist_ok=True)
        # (self.user_state_dir / "migrations").mkdir(exist_ok=True)
        # (self.user_state_dir / "tasks").mkdir(exist_ok=True)
        # self.index_path = self.user_state_dir / "registry.json"
        # self.lock_path = self.user_state_dir / "registry.lock"

        self.project_root = self._resolve_project_root(self.root)
        self.project_journal = self.project_root / ".maestro" / "project-state.jsonl"
        self.config = self._load_config()
        self.migration_marker = self.user_state_dir / "migrations" / f"{self._project_key(self.project_root)}.{self.config['storage_backend']}.json"
        self.memvara_migration_marker = self.user_state_dir / "migrations" / f"{self._project_key(self.project_root)}.memvara.json"
        self.mem = _MemvaraState(self.user_state_dir) if self.config["storage_backend"] == "memvara" else _FileState(self.user_state_dir)
        self._migrate_legacy_project_journal()

    @staticmethod
    def _resolve_workspace(root: str | Path) -> Path:
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {path}")
        return path

    @staticmethod
    def _project_key(project: Path) -> str:
        import hashlib
        return hashlib.sha256(str(project).encode()).hexdigest()[:24]

    @staticmethod
    def git_root(workspace: str | Path) -> Path:
        result = subprocess.run(["git", "-C", str(Path(workspace).resolve()), "rev-parse", "--show-toplevel"], text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError(f"Workspace is not a Git repository: {workspace}")
        return Path(result.stdout.strip()).resolve()

    @staticmethod
    def _resolve_project_root(workspace: Path) -> Path:
        result = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True, capture_output=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            common = Path(result.stdout.strip()).resolve()
            if common.name == ".git":
                return common.parent.resolve()
        return Maestro.git_root(workspace) if (workspace / ".git").exists() else workspace

    @contextmanager
    def _task_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
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
        merged: dict[str, Any] = {}
        config_paths = [
            self.user_state_dir / "config.toml",
            self.project_root / ".maestro" / "config.toml",
            self.root / ".maestro" / "config.toml",
        ]
        seen: set[Path] = set()
        context_layers: list[tuple[str, dict[str, Any]]] = []
        for idx, path in enumerate(config_paths):
            path = path.resolve()
            if path in seen or not path.exists():
                continue
            seen.add(path)
            try:
                with path.open("rb") as fh:
                    value = tomllib.load(fh)
                if isinstance(value, dict):
                    ctx = value.get("context")
                    if isinstance(ctx, dict):
                        # Parsed per file (below) so each entry's source is stamped.
                        context_layers.append(("user config" if idx == 0 else "project config", ctx))
                    for key, item in value.items():
                        if isinstance(item, dict) and isinstance(merged.get(key), dict):
                            merged[key] = {**merged[key], **item}
                        else:
                            merged[key] = item
            except (OSError, tomllib.TOMLDecodeError):
                continue
        codex = merged.get("codex") if isinstance(merged.get("codex"), dict) else {}
        verification = merged.get("verification") if isinstance(merged.get("verification"), dict) else {}
        storage = merged.get("storage") if isinstance(merged.get("storage"), dict) else {}
        backend = str(storage.get("backend") or os.environ.get("MAESTRO_STORAGE", "filesystem")).lower()
        if backend not in {"filesystem", "memvara"}:
            raise ValueError(f"Unsupported storage backend: {backend}")
        effort = codex.get("effort") or os.environ.get("MAESTRO_CODEX_EFFORT")
        if effort is not None:
            effort = str(effort).lower()
            if effort not in {"low", "medium", "high", "xhigh", "max"}:
                raise ValueError(f"Unsupported Codex reasoning effort: {effort}")
        command = verification.get("command")
        if isinstance(command, str):
            command = [x for x in command.split() if x]
        elif command is not None and not (isinstance(command, list) and all(isinstance(x, str) for x in command)):
            raise ValueError("Verification command must be a string or list of strings")
        from .context import parse_context_config
        from .modes import parse_modes

        context: dict[str, Any] = {}
        for source, table in context_layers:
            context.update(parse_context_config(table, source))

        return {"model": codex.get("model") or os.environ.get("MAESTRO_CODEX_MODEL"), "effort": effort, "verification_command": command, "storage_backend": backend, "modes": parse_modes(merged.get("modes")), "context": context}

    def codex_defaults(self) -> dict[str, Any]:
        return {"model": self.config.get("model"), "effort": self.config.get("effort")}

    def close(self) -> None:
        self.mem.close()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _subject(self, task_id: str) -> str:
        return f"maestro:task:{task_id}"

    def _load_index(self) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return []
        return data if isinstance(data, list) else []

    def _save_index(self, items: list[dict[str, Any]]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.index_path)

    def _registry_records(self) -> list[dict[str, Any]]:
        if self.config["storage_backend"] == "filesystem":
            return sorted(self._load_index(), key=lambda x: int(x.get("number", 0)))
        out: dict[str, dict[str, Any]] = {}
        for claim in self.mem.history(self._REGISTRY_SUBJECT, self._REGISTRY_PREDICATE):
            try:
                item = json.loads(claim.object)
                if isinstance(item, dict) and item.get("task_id"):
                    item["number"] = int(item["number"]); out[str(item["task_id"])] = item
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return sorted(out.values(), key=lambda x: int(x["number"]))

    def _registry_record(self, task_id: str, number: int, title: str, created_at: str | None = None) -> dict[str, Any]:
        return {"number": number, "task_id": task_id, "title": title, "created_at": created_at or self._now().isoformat(), "workspace": str(self.root), "project_root": str(self.project_root)}

    def _write_registry_record(self, record: dict[str, Any]) -> None:
        if self.config["storage_backend"] == "filesystem":
            items = {str(x["task_id"]): x for x in self._load_index()}
            items[str(record["task_id"])] = dict(record)
            self._save_index(sorted(items.values(), key=lambda x: int(x.get("number", 0))))
        else:
            now = self._now()
            self.mem.remember(self._REGISTRY_SUBJECT, self._REGISTRY_PREDICATE, json.dumps(record, sort_keys=True), valid_from=now, recorded_at=now)

    def _write_claim(self, task_id: str, predicate: str, value: str, episode_ids: list[str] | None = None) -> None:
        now = self._now()
        self.mem.remember(self._subject(task_id), predicate, str(value), sources=[], valid_from=now, recorded_at=now, episode_ids=episode_ids or [])

    def _claims(self, task_id: str) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for predicate in ("task_status", "task_owner", "task_implementer", "task_design", "task_result", "task_verification", "task_workspace", "task_model", "task_effort", "task_number", "task_title", "task_origin_agent", "task_target_agent", "task_branch", "task_request", "task_runtime", "task_gates"):
            claims = self.mem.history(self._subject(task_id), predicate)
            if claims:
                mapping[predicate] = str(claims[-1].object)
        return mapping

    def _migrate_legacy_project_journal(self) -> None:
        project_journal = self.project_root / ".maestro" / "project-state.jsonl"
        if self.migration_marker.exists() or not project_journal.is_file():
            return
        try:
            lines = project_journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        records: dict[str, dict[str, Any]] = {}; claims: dict[str, dict[str, Any]] = {}
        for line in lines:
            try: event = json.loads(line)
            except json.JSONDecodeError: continue
            if not isinstance(event, dict): continue
            tid = event.get("task_id")
            if not isinstance(tid, str):
                record = event.get("record") if event.get("kind") == "registry" else None
                tid = record.get("task_id") if isinstance(record, dict) else None
            if not isinstance(tid, str) or not tid.startswith("task-"): continue
            if event.get("kind") == "registry" and isinstance(event.get("record"), dict):
                records[tid] = {**records.get(tid, {}), **event["record"]}
            elif event.get("kind") == "claim":  # pragma: no branch
                claims.setdefault(tid, {})[str(event.get("predicate"))] = event.get("value")
        existing = self._registry_records()
        next_number = max([int(x.get("number", 0)) for x in existing] + [0]) + 1
        existing_ids = {str(x["task_id"]) for x in existing}
        field_map = {"task_status":"phase", "task_owner":"supervisor", "task_implementer":"implementer", "task_design":"design", "task_result":"result", "task_verification":"verification", "task_workspace":"workspace", "task_model":"model", "task_effort":"effort"}
        imported = 0
        for tid in sorted(set(records) | set(claims)):
            if tid in existing_ids: continue  # pragma: no branch
            rec = records.get(tid, {}); c = claims.get(tid, {})
            title = str(c.get("task_title") or rec.get("title") or tid)
            workspace = str(c.get("task_workspace") or rec.get("workspace") or self.root)
            new_record = {"number": next_number, "task_id": tid, "title": title, "created_at": str(rec.get("created_at") or self._now().isoformat()), "workspace": workspace, "project_root": str(self.project_root), "legacy_task_number": c.get("task_number") or rec.get("task_number") or rec.get("number")}
            self._write_registry_record(new_record)
            for predicate in field_map:
                if c.get(predicate) not in (None, ""): self._write_claim(tid, predicate, str(c[predicate]))
            self._write_claim(tid, "task_number", str(next_number)); self._write_claim(tid, "task_title", title)
            next_number += 1; imported += 1
        self.migration_marker.write_text(json.dumps({"imported": imported, "source": str(self.project_journal), "migrated_at": self._now().isoformat()}, indent=2), encoding="utf-8")

    def _migrate_legacy_memvara(self, strict: bool = False) -> int:
        if self.config["storage_backend"] != "filesystem" or self.memvara_migration_marker.exists(): return 0  # pragma: no branch
        legacy_db = self.root / ".maestro" / "memory.db"
        if not legacy_db.is_file(): return 0
        try:
            legacy = _MemvaraState(self.root / ".maestro", "memory.db"); claims = legacy.get_all()
        except Exception as exc:
            if strict: raise RuntimeError(f"Unable to migrate legacy Memvara state: {exc}") from exc
            return 0
        imported=0; existing={(c.subject,c.predicate,c.object) for c in self.mem.get_all()}
        try:
            for claim in claims:
                subject=getattr(claim,"subject",None); predicate=getattr(claim,"predicate",None); obj=getattr(claim,"object",None)
                if not isinstance(subject,str) or not isinstance(predicate,str): continue  # pragma: no branch
                if not (subject==self._REGISTRY_SUBJECT or subject.startswith("maestro:task:")): continue  # pragma: no branch
                key=(subject,predicate,str(obj))
                if key in existing: continue  # pragma: no branch
                self.mem.remember(subject,predicate,str(obj)); existing.add(key); imported+=1
        finally:
            legacy.close()
        self.memvara_migration_marker.write_text(json.dumps({"imported_claims":imported,"source":str(legacy_db),"migrated_at":self._now().isoformat()},indent=2),encoding="utf-8")
        return imported

    def migrate_legacy_memvara(self) -> dict[str, Any]:
        imported=self._migrate_legacy_memvara(strict=True)
        return {"backend":self.config["storage_backend"],"imported_claims":imported,"marker":str(self.memvara_migration_marker),"migrated":self.memvara_migration_marker.exists()}

    def _new_task_number(self) -> int:
        return max([int(x.get("number",0)) for x in self._registry_records()] + [0]) + 1

    def _register_task(self, task_id: str, title: str, number: int) -> None:
        self._write_registry_record(self._registry_record(task_id, number, title))

    def resolve_task(self, ref: str) -> str:
        ref=str(ref).strip(); items=self._registry_records()
        if ref.isdigit():
            number=int(ref)
            matches=[x for x in items if int(x.get("number",0))==number]
            if matches:  # pragma: no branch
                return str(matches[0]["task_id"])
            raise KeyError(f"Unknown task number {ref}. Available task numbers: {', '.join(str(x.get('number')) for x in items) or 'none'}")
        if ref.startswith("task-") and any(str(x.get("task_id"))==ref for x in items): return ref
        if ref.startswith("task-") and self.mem.history(self._subject(ref), "task_status"):  # pragma: no branch
            return ref
        raise KeyError(f"Unknown task reference {ref!r}. Run `maestro list` to see available tasks.")

    @staticmethod
    def _workspace_from_artifact_path(value: str | None) -> str | None:
        if not value:  # pragma: no branch
            return None
        path=Path(value).expanduser().resolve()
        for parent in (path,*path.parents):
            if parent.name==".maestro": return str(parent.parent)
        return None

    def status(self, task_ref: str) -> dict[str, Any]:
        task_id=self.resolve_task(task_ref)
        index=next((x for x in self._registry_records() if str(x["task_id"])==task_id),None)
        claims=self._claims(task_id)
        result={"task_id":task_id,"task_number":index.get("number") if index else None,"title":index.get("title") if index else None}
        result.update({"phase":claims.get("task_status"),"supervisor":claims.get("task_owner"),"implementer":claims.get("task_implementer"),"design":claims.get("task_design"),"result":claims.get("task_result"),"verification":claims.get("task_verification"),"workspace":claims.get("task_workspace") or (index or {}).get("workspace"),"model":claims.get("task_model"),"effort":claims.get("task_effort"),"origin_agent":claims.get("task_origin_agent"),"target_agent":claims.get("task_target_agent"),"branch":claims.get("task_branch")})
        if claims.get("task_number"):  # pragma: no branch
            try: result["task_number"]=int(claims["task_number"])
            except ValueError: pass
        if claims.get("task_title"): result["title"]=claims["task_title"]
        gates_claim = claims.get("task_gates")
        if gates_claim:
            try: parsed_gates = json.loads(gates_claim)
            except (ValueError, TypeError): parsed_gates = None
            if isinstance(parsed_gates, dict):
                verdicts = parsed_gates.get("verdicts")
                if isinstance(verdicts, dict):
                    result["gates"] = {role: {"agent": v.get("agent"), "ok": v.get("ok"), "issue_count": len(v.get("issues") or [])} for role, v in verdicts.items() if isinstance(v, dict)}
                if parsed_gates.get("bounces") is not None:
                    result["bounces"] = parsed_gates["bounces"]
        if not result["workspace"]:  # pragma: no branch
            for key in ("design","result","verification"):
                result["workspace"]=self._workspace_from_artifact_path(result[key])
                if result["workspace"]:  # pragma: no branch
                    break
        result["project_root"]=(index or {}).get("project_root") or str(self.project_root)
        return result

    def list_tasks(self, *, workspace_filter: str | None = None, project_filter: str | None = None) -> list[dict[str, Any]]:
        output=[]
        for item in self._registry_records():
            try: record=self.status(str(item["task_id"]))
            except KeyError: continue
            if workspace_filter and record.get("workspace") != str(Path(workspace_filter).expanduser().resolve()):  # pragma: no branch
                continue
            if project_filter and record.get("project_root") != str(Path(project_filter).expanduser().resolve()): continue  # pragma: no branch
            output.append(record)
        return sorted(output,key=lambda x:int(x.get("task_number") or x.get("number") or 0))

