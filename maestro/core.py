from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


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


class RegistryUnreadableError(OSError):
    """registry.json exists but could not be read at this moment, for example
    because the process has too many open files or no permission to read it.
    The registry is not rebuilt and not overwritten in that case."""


_HELD_LOCKS = threading.local()
_LOCK_WARNED: set[str] = set()


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``path`` (created if missing).

    The lock is taken on a separate lock file, never on the data file itself,
    so it still works after the data file is replaced by a rewrite.

    A thread that already holds the lock can take it again (for example a
    registry repair inside a registration); a second flock from the same
    thread would otherwise wait for itself forever. Other threads and other
    processes still wait.

    When locking is not available (no ``fcntl`` on Windows, or a file system
    such as some NFS and SMB mounts that refuses flock), this continues
    without a lock and prints a warning once per lock file.
    """
    held: dict[str, int] = _HELD_LOCKS.__dict__.setdefault("paths", {})
    key = str(path)
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    locked = False
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError) as exc:
            if key not in _LOCK_WARNED:
                _LOCK_WARNED.add(key)
                print(f"[maestro] warning: {path} is not locked because file locking is not available here ({exc}); do not run `maestro gc` or a second writer while the daemon is running", file=sys.stderr)
        held[key] = 1
        try:
            yield
        finally:
            del held[key]
    finally:
        try:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _replace_atomically(path: Path, text: str) -> None:
    """Write ``text`` to a unique temporary file next to ``path``, then rename
    it over ``path``. Two writers never share a temporary file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _claim_line(claim: _Claim) -> str:
    return json.dumps({"subject": claim.subject, "predicate": claim.predicate, "object": claim.object, "episode_ids": claim.episode_ids or []}, ensure_ascii=False) + "\n"


class _FileState:
    """Small append-only claim store at user scope.

    Appends and rewrites (``forget``) take the same lock file, so a rewrite
    never drops a claim another process appends while it runs. Parsed claims
    are cached and re-read only when the journal changes on disk, because
    listing tasks asks for many claims of many tasks.
    """

    _TAIL_BYTES = 512  # how much of the file's end is compared to detect a change

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "state.jsonl"
        self.lock_path = state_dir / "state.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._cache_lock = threading.Lock()
        self._cache_key: tuple[int, int, int, int, bytes] | None = None
        self._cache_claims: list[_Claim] = []
        self._cache_index: dict[tuple[str, str], list[_Claim]] = {}

    def _parse(self) -> list[_Claim]:
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

    def _load(self) -> tuple[list[_Claim], dict[tuple[str, str], list[_Claim]]]:
        """The parsed journal and a (subject, predicate) index, re-read only
        when the file changed.

        The file counts as unchanged only when its inode, size, modification
        time, change time and last few hundred bytes are all the same. The
        stat values alone are not enough: on Linux ext4 an inode number is
        reused after a rewrite and timestamps can be coarse, so a rewritten
        journal of the same size can report the same values.
        """
        try:
            with self.path.open("rb") as fh:
                st = os.fstat(fh.fileno())
                fh.seek(max(0, st.st_size - self._TAIL_BYTES))
                tail = fh.read(self._TAIL_BYTES)
            key: tuple[int, int, int, int, bytes] | None = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, tail)
        except OSError:
            key = None
        with self._cache_lock:
            if key is None or key != self._cache_key:
                claims = self._parse()
                index: dict[tuple[str, str], list[_Claim]] = {}
                for claim in claims:
                    index.setdefault((claim.subject, claim.predicate), []).append(claim)
                self._cache_key, self._cache_claims, self._cache_index = key, claims, index
            return self._cache_claims, self._cache_index

    def _read(self) -> list[_Claim]:
        return list(self._load()[0])

    def _append(self, claim: _Claim) -> None:
        with _file_lock(self.lock_path):
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(_claim_line(claim))
                fh.flush()

    def remember(self, subject: str, predicate: str, object: str, **_: Any) -> None:
        self._append(_Claim(subject, predicate, str(object), [uuid.uuid4().hex]))

    def history(self, subject: str, predicate: str) -> list[_Claim]:
        return list(self._load()[1].get((subject, predicate), []))

    def get_all(self) -> list[_Claim]:
        return self._read()

    def forget(self, subject: str) -> int:
        """Rewrite the journal without any claim for ``subject``; returns how many were dropped."""
        with _file_lock(self.lock_path):
            lines = self._parse()
            kept = [c for c in lines if c.subject != subject]
            dropped = len(lines) - len(kept)
            if dropped:
                _replace_atomically(self.path, "".join(_claim_line(c) for c in kept))
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
        try:
            result = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True, capture_output=True, check=False)
        except (OSError, subprocess.SubprocessError):
            return workspace  # git unavailable: treat the workspace as its own root
        if result.returncode == 0 and result.stdout.strip():
            common = Path(result.stdout.strip()).resolve()
            if common.name == ".git":
                return common.parent.resolve()
        return Maestro.git_root(workspace) if (workspace / ".git").exists() else workspace

    @contextmanager
    def _task_lock(self) -> Iterator[None]:
        with _file_lock(self.lock_path):
            yield

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
        defaults = self._parse_defaults(merged.get("defaults"))
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
        from .knowledge import parse_continuation
        from .modes import parse_modes

        context: dict[str, Any] = {}
        for source, table in context_layers:
            context.update(parse_context_config(table, source))

        return {"model": codex.get("model") or os.environ.get("MAESTRO_CODEX_MODEL"), "effort": effort, "verification_command": command, "storage_backend": backend, "modes": parse_modes(merged.get("modes")), "context": context, "defaults": defaults, "continuation": parse_continuation(merged.get("continuation"))}

    @staticmethod
    def _parse_defaults(raw: Any) -> dict[str, Any]:
        """Parse and validate the ``[defaults]`` table from merged config.

        Project-level routing defaults consulted at delegate time when a handoff
        names no target agent: ``agent`` (default implementer), ``fallback``
        (list of fallback agents), ``model`` / ``effort`` (applied to the chosen
        agent when the handoff sets none). ``max_parallel`` is the number of tasks that may
        run turns at the same time for one workspace (see
        docs/design-parallel-tasks.md); the daemon uses 4 when it is absent. Absent table -> empty dict; invalid
        values raise so misconfiguration fails at daemon start, not mid-delegation.
        """
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ValueError("[defaults] must be a table")
        unknown = set(raw) - {"agent", "fallback", "model", "effort", "max_parallel"}
        if unknown:
            raise ValueError(f"[defaults] has unknown keys: {', '.join(sorted(unknown))}")
        out: dict[str, Any] = {}
        for key in ("agent", "model"):
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"[defaults] {key} must be a non-empty string")
                out[key] = value.strip()
        fallback = raw.get("fallback")
        if fallback is not None:
            if not isinstance(fallback, list) or not all(isinstance(x, str) and x.strip() for x in fallback):
                raise ValueError("[defaults] fallback must be a list of non-empty agent names")
            out["fallback"] = [x.strip() for x in fallback]
        effort = raw.get("effort")
        if effort is not None:
            effort = str(effort).lower()
            if effort not in {"low", "medium", "high", "xhigh", "max"}:
                raise ValueError(f"Unsupported default reasoning effort: {effort}")
            out["effort"] = effort
        max_parallel = raw.get("max_parallel")
        if max_parallel is not None:
            if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel < 1:
                raise ValueError("[defaults] max_parallel must be a whole number of at least 1")
            out["max_parallel"] = max_parallel
        return out

    def codex_defaults(self) -> dict[str, Any]:
        return {"model": self.config.get("model"), "effort": self.config.get("effort")}

    def close(self) -> None:
        self.mem.close()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _subject(self, task_id: str) -> str:
        return f"maestro:task:{task_id}"

    def _read_index_file(self) -> list[dict[str, Any]] | str:
        """registry.json as a list, or "missing" when there is no file, or
        "damaged" when its contents are not a JSON list. Any other read error
        raises RegistryUnreadableError: the file may be fine, so it must not
        be rebuilt or overwritten."""
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            raise RegistryUnreadableError(f"Could not read the task registry {self.index_path} ({exc}); nothing was changed. Try again.") from exc
        except ValueError:  # not JSON, or not UTF-8
            return "damaged"
        return data if isinstance(data, list) else "damaged"

    def _load_index(self) -> list[dict[str, Any]]:
        """The task registry (registry.json).

        A missing or damaged file is repaired under the registry lock. The
        file is read again under the lock, because another process may have
        repaired it in the meantime. A file that is still damaged is moved
        aside, never overwritten, and kept for inspection. The registry is
        then rebuilt from the tasks' claims and saved, so the rebuild runs
        once and not on every listing. Returning an empty list instead would
        make the next registration write a registry holding only the new task,
        deleting every other entry and restarting numbering.
        """
        items = self._read_index_file()
        if isinstance(items, list):
            return items
        with self._task_lock():
            items = self._read_index_file()
            if isinstance(items, list):
                return items
            if items == "damaged":
                aside = self.index_path.with_name(f"registry.corrupt-{self._now().strftime('%Y%m%dT%H%M%S%f')}.json")
                try:
                    os.replace(self.index_path, aside)
                    print(f"[maestro] {self.index_path} could not be parsed; moved it to {aside} and rebuilt the task registry from task claims", file=sys.stderr)
                except FileNotFoundError:
                    pass  # removed by a process that does not take the lock (an older version)
            records = self._index_from_claims()
            self._save_index(records)
            return records

    def _created_at_from_disk(self, task_id: str) -> str | None:
        """When a task was created, for a rebuilt registry record. The claim
        journal stores no times, so this uses the date and time in the task
        id (task-YYYYMMDD-HHMMSS-..., local time), or else the modification
        time of the task's folder."""
        try:
            return datetime.strptime(task_id[len("task-"):len("task-") + 15], "%Y%m%d-%H%M%S").astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
        try:
            return datetime.fromtimestamp((self.user_state_dir / "tasks" / task_id).stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            return None

    def _index_from_claims(self) -> list[dict[str, Any]]:
        """Registry records rebuilt from the tasks' claims. Called under the
        registry lock.

        Every task with at least one claim is kept. A task keeps the number in
        its task_number claim. A task without a usable one gets the next number
        after the highest ever given out, and that number is written as a claim
        so a later rebuild gives the same number.
        """
        prefix = self._subject("")
        task_ids: dict[str, None] = {}
        numbers: dict[str, int] = {}
        for claim in self.mem.get_all():
            if not claim.subject.startswith(prefix):
                continue
            task_id = claim.subject[len(prefix):]
            task_ids[task_id] = None
            if claim.predicate == "task_number":
                try:
                    numbers[task_id] = int(claim.object)
                except ValueError:
                    continue
        next_number = max([*numbers.values(), self._read_task_counter()]) + 1
        for task_id in sorted(t for t in task_ids if t not in numbers):
            numbers[task_id] = next_number
            self._write_claim(task_id, "task_number", str(next_number))
            next_number += 1
        records = []
        for task_id, number in numbers.items():
            claims = self._claims(task_id)
            workspace = claims.get("task_workspace") or ""
            project_root = str(self._resolve_project_root(Path(workspace))) if workspace and Path(workspace).is_dir() else workspace
            record = {"number": number, "task_id": task_id, "title": claims.get("task_title") or task_id, "created_at": self._created_at_from_disk(task_id), "workspace": workspace, "project_root": project_root}
            legacy = self.mem.history(self._subject(task_id), "task_legacy_number")
            if legacy:
                try:
                    record["legacy_task_number"] = json.loads(legacy[-1].object)
                except ValueError:
                    record["legacy_task_number"] = legacy[-1].object
            records.append(record)
        return sorted(records, key=lambda x: int(x["number"]))

    def _save_index(self, items: list[dict[str, Any]]) -> None:
        _replace_atomically(self.index_path, json.dumps(items, indent=2, ensure_ascii=False))
        self._bump_task_counter(max([int(x.get("number", 0)) for x in items] + [0]))

    def _read_task_counter(self) -> int:
        try:
            return int((self.user_state_dir / "task-counter").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _bump_task_counter(self, number: int) -> None:
        """Remember the highest task number ever given out, so a number is
        never reused after gc removes the newest task."""
        if number > self._read_task_counter():
            _replace_atomically(self.user_state_dir / "task-counter", str(number))

    def unregister_task(self, task_id: str, should_remove: Callable[[dict[str, Any] | None], bool] | None = None) -> int | None:
        """Remove a task from the registry and drop its claims (used by gc).

        Everything happens under the registry lock and the claim journal lock,
        so a task registered by a daemon at the same moment is never lost, and
        no claim can be written for this task between the check and the
        removal. ``should_remove`` receives the task's current registry record
        (None if it is no longer registered) and is evaluated under both
        locks; when it returns False nothing changes and None is returned.
        gc uses it to confirm that the task is still finished and still old.
        Otherwise returns the number of claims dropped. The memvara backend
        keeps claims as history and cannot drop them, so it is refused before
        anything changes.
        """
        if self.config["storage_backend"] != "filesystem":
            raise ValueError("Removing tasks is not supported on the memvara storage backend yet; nothing was changed")
        with self._task_lock(), _file_lock(self.mem.lock_path):
            items = self._load_index()
            if should_remove is not None and not should_remove(next((x for x in items if str(x.get("task_id")) == task_id), None)):
                return None
            self._save_index([x for x in items if str(x.get("task_id")) != task_id])
            return self.mem.forget(self._subject(task_id))

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

    def _registry_record(self, task_id: str, number: int, title: str, created_at: str | None = None, project_root: str | None = None) -> dict[str, Any]:
        return {"number": number, "task_id": task_id, "title": title, "created_at": created_at or self._now().isoformat(), "workspace": str(self.root), "project_root": str(project_root or self.project_root)}

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
        for predicate in ("task_status", "task_owner", "task_implementer", "task_design", "task_result", "task_verification", "task_workspace", "task_model", "task_effort", "task_number", "task_title", "task_origin_agent", "task_target_agent", "task_branch", "task_base_head", "task_python_test_suite", "task_request", "task_runtime", "task_gates", "task_knowledge", "task_run_dir", "task_run_dir_kind", "task_run_dir_removed"):
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
        field_map = {"task_status":"phase", "task_owner":"supervisor", "task_implementer":"implementer", "task_design":"design", "task_result":"result", "task_verification":"verification", "task_workspace":"workspace", "task_model":"model", "task_effort":"effort"}
        imported = 0
        # Numbers come from _new_task_number under the registry lock, like a
        # daemon registration, so a number removed by gc is never given out again.
        with self._task_lock():
            existing_ids = {str(x["task_id"]) for x in self._registry_records()}
            for tid in sorted(set(records) | set(claims)):
                if tid in existing_ids: continue  # pragma: no branch
                rec = records.get(tid, {}); c = claims.get(tid, {})
                title = str(c.get("task_title") or rec.get("title") or tid)
                workspace = str(c.get("task_workspace") or rec.get("workspace") or self.root)
                number = self._new_task_number()
                legacy_number = c.get("task_number") or rec.get("task_number") or rec.get("number")
                new_record = {"number": number, "task_id": tid, "title": title, "created_at": str(rec.get("created_at") or self._now().isoformat()), "workspace": workspace, "project_root": str(self.project_root), "legacy_task_number": legacy_number}
                self._write_registry_record(new_record)
                self._bump_task_counter(number)
                for predicate in field_map:
                    if c.get(predicate) not in (None, ""): self._write_claim(tid, predicate, str(c[predicate]))
                self._write_claim(tid, "task_number", str(number)); self._write_claim(tid, "task_title", title)
                if legacy_number is not None:  # kept as a claim so a registry rebuild restores it
                    self._write_claim(tid, "task_legacy_number", json.dumps(legacy_number))
                imported += 1
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
        highest = max([int(x.get("number",0)) for x in self._registry_records()] + [self._read_task_counter()])
        return highest + 1

    def _register_task(self, task_id: str, title: str, number: int, project_root: str | None = None) -> None:
        self._write_registry_record(self._registry_record(task_id, number, title, project_root=project_root))
        self._bump_task_counter(number)

    def resolve_task(self, ref: str, records: list[dict[str, Any]] | None = None) -> str:
        ref=str(ref).strip(); items=records if records is not None else self._registry_records()
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

    def status(self, task_ref: str, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """One task's state. ``records`` is the registry when the caller has
        already read it (list_tasks), so it is not re-read for every task."""
        if records is None:
            records=self._registry_records()
        task_id=self.resolve_task(task_ref, records)
        index=next((x for x in records if str(x["task_id"])==task_id),None)
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
        # Where the task's work is: the workspace, or the task's own worktree.
        # A task from before run directories existed ran in its workspace.
        result["run_dir"]=claims.get("task_run_dir") or result["workspace"]
        result["run_dir_kind"]=claims.get("task_run_dir_kind") or "workspace"
        if claims.get("task_run_dir_removed")=="true": result["run_dir_removed"]=True
        elif result["run_dir_kind"]=="worktree" and not Path(result["run_dir"]).is_dir():
            # Deleted by hand: the next turn creates it again from the branch.
            result["run_dir_missing"]=True
        # The phase claim reports a parked task as REVIEWING, the same as a
        # finished one. The runtime snapshot says it is waiting, what for, and
        # the question to answer with `maestro task answer`.
        try: runtime=json.loads(claims.get("task_runtime") or "{}")
        except (ValueError, TypeError): runtime={}
        if isinstance(runtime, dict) and runtime.get("state")=="input-required":
            result["state"]="input-required"
            result["awaiting"]=runtime.get("awaiting")
            result["question"]=runtime.get("question")
        return result

    def list_tasks(self, *, workspace_filter: str | None = None, project_filter: str | None = None) -> list[dict[str, Any]]:
        output=[]
        records=self._registry_records()
        for item in records:
            try: record=self.status(str(item["task_id"]), records)
            except KeyError: continue
            if workspace_filter and record.get("workspace") != str(Path(workspace_filter).expanduser().resolve()):  # pragma: no branch
                continue
            if project_filter and record.get("project_root") != str(Path(project_filter).expanduser().resolve()): continue  # pragma: no branch
            output.append(record)
        return sorted(output,key=lambda x:int(x.get("task_number") or x.get("number") or 0))

