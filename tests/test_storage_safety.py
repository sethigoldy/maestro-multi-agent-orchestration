"""The claim journal and task registry survive concurrent writers and damage.

Each test reproduces a defect found in review: gc rewriting the journal while
the daemon appends to it, gc saving the registry while the daemon registers a
task, a registry that could not be parsed being replaced by an empty one, task
numbers given out twice, and `task list` re-reading the whole journal for every
claim of every task.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import maestro.core as core
from maestro.core import Maestro, _FileState, _replace_atomically


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(path))
    return path


def _register(m: Maestro, task_id: str, title: str, workspace: Path) -> int:
    with m._task_lock():
        number = m._new_task_number()
        m._register_task(task_id, title, number, project_root=str(workspace))
    m._write_claim(task_id, "task_number", str(number))
    m._write_claim(task_id, "task_title", title)
    m._write_claim(task_id, "task_workspace", str(workspace))
    m._write_claim(task_id, "task_status", "COMPLETE")
    return number


# ------------------------------------------------------------ journal
def test_forget_keeps_claims_appended_by_another_process(home, tmp_path):
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from maestro.core import _FileState\n"
        "state = _FileState(Path(sys.argv[1]))\n"
        "for i in range(1500):\n"
        "    state.remember('keep', 'n', str(i))\n",
        encoding="utf-8",
    )
    state = _FileState(home)
    # A large journal makes each rewrite slow enough to overlap the appends.
    filler = "".join(json.dumps({"subject": f"filler-{i}", "predicate": "n", "object": "x", "episode_ids": []}) + "\n" for i in range(30000))
    state.path.write_text(filler, encoding="utf-8")
    for i in range(50):
        state.remember(f"drop-{i}", "n", "x")
    package_root = str(Path(core.__file__).resolve().parent.parent)  # the code under test, not an installed copy
    env = dict(os.environ, PYTHONPATH=package_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, str(writer), str(home)], env=env)
    i = 0
    while proc.poll() is None:
        state.forget(f"drop-{i % 50}")
        i += 1
    assert proc.wait() == 0
    kept = [c.object for c in state.history("keep", "n")]
    assert kept == [str(n) for n in range(1500)]  # every append survived, in order


def test_history_sees_writes_from_another_instance_and_rewrites(home):
    reader = _FileState(home)
    writer = _FileState(home)
    assert reader.history("s", "p") == []
    writer.remember("s", "p", "1")
    assert [c.object for c in reader.history("s", "p")] == ["1"]
    writer.remember("s", "p", "2")
    assert [c.object for c in reader.history("s", "p")] == ["1", "2"]
    writer.forget("s")
    assert reader.history("s", "p") == []
    assert reader.forget("s") == 0  # nothing left to drop: the file is not rewritten


def test_history_when_the_journal_cannot_be_read(home, monkeypatch):
    state = _FileState(home)
    state.remember("s", "p", "1")
    state.path.unlink()
    assert state.history("s", "p") == []  # stat fails: re-read, which finds nothing
    state.path.mkdir()  # a directory where the journal should be: reading fails
    assert state.get_all() == []


def test_replace_atomically_cleans_up_after_a_failure(tmp_path, monkeypatch):
    target = tmp_path / "registry.json"
    monkeypatch.setattr(core.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        _replace_atomically(target, "{}")
    assert list(tmp_path.iterdir()) == []  # no temporary file left behind


# ------------------------------------------------------------ registry
def test_gc_unregister_waits_for_a_registration_in_progress(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    other = Maestro(home)
    try:
        _register(m, "task-old", "old", ws)
        registered = threading.Event()

        def register_slowly():
            with other._task_lock():
                number = other._new_task_number()
                time.sleep(0.5)  # gc tries to save the registry right now
                other._register_task("task-new", "new", number, project_root=str(ws))
            registered.set()

        thread = threading.Thread(target=register_slowly)
        thread.start()
        time.sleep(0.1)
        m.unregister_task("task-old")
        thread.join()
        ids = [x["task_id"] for x in m._registry_records()]
        assert ids == ["task-new"]  # the new task survived; the old one is gone
        assert m.mem.history(m._subject("task-old"), "task_status") == []
    finally:
        m.close()
        other.close()


def test_a_corrupt_registry_is_set_aside_and_rebuilt(home, tmp_path, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        for n in range(3):
            _register(m, f"task-{n}", f"title {n}", ws)
        m.index_path.write_text("[{broken", encoding="utf-8")
        rebuilt = m._registry_records()
        assert [(x["number"], x["task_id"], x["title"]) for x in rebuilt] == [(1, "task-0", "title 0"), (2, "task-1", "title 1"), (3, "task-2", "title 2")]
        assert list(home.glob("registry.corrupt-*.json")), "the damaged file is kept for inspection"
        assert "rebuilt the task registry" in capsys.readouterr().err
        assert m.resolve_task("2") == "task-1"
        assert _register(m, "task-3", "next", ws) == 4  # numbering continues; nothing was lost
        assert len(m._registry_records()) == 4
    finally:
        m.close()


def test_registry_that_is_not_a_list_or_unreadable(home, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        m.index_path.write_text('{"not": "a list"}', encoding="utf-8")
        assert [x["task_id"] for x in m._registry_records()] == ["task-a"]
        _register(m, "task-b", "b", ws)
        real_read = Path.read_text

        def failing_read(self, *a, **k):
            if self == m.index_path:
                raise OSError("unreadable")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", failing_read)
        assert [x["task_id"] for x in m._registry_records()] == ["task-a", "task-b"]
    finally:
        m.close()


def test_a_missing_registry_is_rebuilt_from_claims(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        m._write_claim("task-weird", "task_number", "not-a-number")  # skipped
        m._write_claim("task-gone", "task_number", "7")  # no workspace claim
        m.index_path.unlink()
        records = {x["task_id"]: x for x in m._registry_records()}
        assert set(records) == {"task-a", "task-gone"}
        assert records["task-a"]["project_root"] == str(ws.resolve()) or records["task-a"]["project_root"] == str(ws)
        assert records["task-gone"]["workspace"] == ""
    finally:
        m.close()


def test_the_registry_moved_by_another_process_first(home, tmp_path, monkeypatch):
    m = Maestro(home)
    try:
        m.index_path.write_text("[{broken", encoding="utf-8")
        real_replace = os.replace

        def moved_already(src, dst):
            if Path(src) == m.index_path:
                raise FileNotFoundError(src)
            return real_replace(src, dst)

        monkeypatch.setattr(core.os, "replace", moved_already)
        assert m._registry_records() == []
    finally:
        m.close()


def test_task_numbers_are_not_reused_after_gc(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        for n in range(3):
            _register(m, f"task-{n}", str(n), ws)
        m.unregister_task("task-2")  # removes the newest task, #3
        assert _register(m, "task-3", "3", ws) == 4
        (home / "task-counter").write_text("garbage", encoding="utf-8")
        assert m._read_task_counter() == 0
        assert m._new_task_number() == 5  # still above every number in the registry
    finally:
        m.close()


def test_unregister_is_refused_on_the_memvara_backend(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        m.config["storage_backend"] = "memvara"
        with pytest.raises(ValueError, match="not supported on the memvara storage backend"):
            m.unregister_task("task-a")
        m.config["storage_backend"] = "filesystem"
        assert [x["task_id"] for x in m._registry_records()] == ["task-a"]
        assert m.mem.history(m._subject("task-a"), "task_status")
    finally:
        m.close()


def test_gc_command_reports_the_memvara_refusal(home, tmp_path, monkeypatch, capsys):
    from maestro import cli

    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    _register(m, "task-a", "a", ws)
    m.close()
    real_init = Maestro.__init__

    def memvara_config(self, *a, **k):
        real_init(self, *a, **k)
        self.config["storage_backend"] = "memvara"

    monkeypatch.setattr(Maestro, "__init__", memvara_config)
    monkeypatch.setattr(Maestro, "_registry_records", lambda self: [{"task_id": "task-a", "number": 1, "created_at": "2000-01-01T00:00:00+00:00"}])
    assert cli.main(["gc", "--days", "1"]) == 2
    assert "not supported on the memvara storage backend" in capsys.readouterr().err


# ------------------------------------------------------------ listing
def test_task_list_reads_the_journal_once(home, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        for n in range(200):
            _register(m, f"task-{n:03d}", f"t{n}", ws)
            for k in range(8):
                m._write_claim(f"task-{n:03d}", "task_runtime", json.dumps({"state": "completed", "k": k}))
        parses = []
        real_parse = _FileState._parse
        monkeypatch.setattr(_FileState, "_parse", lambda self: parses.append(1) or real_parse(self))
        started = time.monotonic()
        tasks = m.list_tasks()
        assert len(tasks) == 200
        assert len(parses) <= 1  # one parse at most, not one per claim
        assert time.monotonic() - started < 10
    finally:
        m.close()
