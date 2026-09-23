"""The claim journal and task registry survive concurrent writers and damage.

Each test reproduces a defect found in review: gc rewriting the journal while
the daemon appends to it, gc saving the registry while the daemon registers a
task, a registry that could not be parsed being replaced by an empty one, task
numbers given out twice, and `task list` re-reading the whole journal for every
claim of every task.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
                raise OSError(errno.EMFILE, "Too many open files")
            return real_read(self, *a, **k)

        before = m.index_path.read_bytes()
        set_aside = sorted(home.glob("registry.corrupt-*.json"))  # the "not a list" file above
        monkeypatch.setattr(Path, "read_text", failing_read)
        # A file that exists but cannot be read right now is not missing and not
        # damaged: nothing is rebuilt, moved or overwritten.
        with pytest.raises(OSError, match="Could not read the task registry"):
            m._registry_records()
        with pytest.raises(OSError, match="Could not read the task registry"):
            m._write_registry_record(m._registry_record("task-c", 3, "c"))
        assert m.index_path.read_bytes() == before
        assert sorted(home.glob("registry.corrupt-*.json")) == set_aside
    finally:
        m.close()


def test_a_missing_registry_is_rebuilt_from_claims(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        m._write_claim("task-weird", "task_number", "not-a-number")  # kept, with a fresh number
        m._write_claim("task-gone", "task_number", "7")  # no workspace claim
        m.index_path.unlink()
        records = {x["task_id"]: x for x in m._registry_records()}
        assert set(records) == {"task-a", "task-gone", "task-weird"}
        assert records["task-weird"]["number"] == 8  # after task-gone's 7
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


# ------------------------------------------------------------ review fixes
def test_a_reader_does_not_move_aside_a_registry_another_process_just_repaired(home, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        good = m.index_path.read_text(encoding="utf-8")
        m.index_path.write_text("[{broken", encoding="utf-8")
        real_read = Path.read_text
        reads: list[int] = []

        def repaired_right_after_the_first_read(self, *a, **k):
            text = real_read(self, *a, **k)
            if self == m.index_path and not reads:
                reads.append(1)
                self.write_text(good, encoding="utf-8")  # another process repairs it now
            return text

        monkeypatch.setattr(Path, "read_text", repaired_right_after_the_first_read)
        assert [x["task_id"] for x in m._registry_records()] == ["task-a"]
        assert not list(home.glob("registry.corrupt-*.json")), "the repaired file was moved away"
        assert json.loads(real_read(m.index_path, encoding="utf-8"))[0]["task_id"] == "task-a"
    finally:
        m.close()


def test_the_move_aside_waits_for_the_registry_lock(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    other = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        good = m.index_path.read_text(encoding="utf-8")
        m.index_path.write_text("[{broken", encoding="utf-8")
        result: dict[str, list[str]] = {}

        def read_registry():
            result["ids"] = [x["task_id"] for x in m._registry_records()]

        with other._task_lock():
            reader = threading.Thread(target=read_registry)
            reader.start()
            time.sleep(0.3)  # the reader has seen the damaged file and waits for the lock
            assert not list(home.glob("registry.corrupt-*.json"))
            m.index_path.write_text(good, encoding="utf-8")  # the lock holder repairs it
        reader.join()
        assert result["ids"] == ["task-a"]
        assert not list(home.glob("registry.corrupt-*.json"))
    finally:
        m.close()
        other.close()


def test_a_missing_registry_is_saved_after_the_rebuild(home, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        _register(m, "task-b", "b", ws)
        m.index_path.unlink()
        rebuilds: list[int] = []
        real_rebuild = Maestro._index_from_claims
        monkeypatch.setattr(Maestro, "_index_from_claims", lambda self: rebuilds.append(1) or real_rebuild(self))
        assert [x["task_id"] for x in m.list_tasks()] == ["task-a", "task-b"]
        assert [x["task_id"] for x in m.list_tasks()] == ["task-a", "task-b"]
        assert len(rebuilds) == 1  # the second listing reads the saved registry
        assert [x["task_id"] for x in json.loads(m.index_path.read_text(encoding="utf-8"))] == ["task-a", "task-b"]
    finally:
        m.close()


def test_the_rebuild_keeps_tasks_without_a_number_and_their_creation_time(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        numbered = "task-20260101-120000-aaaaaa"
        _register(m, numbered, "numbered", ws)
        (home / "task-counter").write_text("5", encoding="utf-8")  # numbers 2..5 were given out and gc'd
        unnumbered = "task-20260102-130000-bbbbbb"
        m._write_claim(unnumbered, "task_status", "WORKING")
        m._write_claim(unnumbered, "task_title", "no number yet")
        m._write_claim("task-folder", "task_status", "COMPLETE")
        folder = home / "tasks" / "task-folder"
        folder.mkdir(parents=True)
        os.utime(folder, (1_700_000_000, 1_700_000_000))
        m._write_claim("task-bare", "task_status", "COMPLETE")
        m.mem.add("an event, not a task")  # other subjects in the journal are ignored
        m.index_path.unlink()

        records = {x["task_id"]: x for x in m._registry_records()}
        assert {k: v["number"] for k, v in records.items()} == {numbered: 1, unnumbered: 6, "task-bare": 7, "task-folder": 8}
        assert records[numbered]["created_at"] == datetime(2026, 1, 1, 12, 0, 0).astimezone(timezone.utc).isoformat()
        assert records[unnumbered]["title"] == "no number yet"
        assert records["task-folder"]["created_at"] == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).isoformat()
        assert records["task-bare"]["created_at"] is None
        # The fresh number is saved as a claim, so a second rebuild gives the same number.
        assert m.mem.history(m._subject(unnumbered), "task_number")[-1].object == "6"
        assert m._new_task_number() == 9
    finally:
        m.close()


def test_legacy_journal_import_uses_the_counter_and_the_registry_lock(home, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".maestro").mkdir(parents=True)
    tid = "task-20250101-000000-abcdef"
    unnumbered = "task-20250102-000000-fedcba"
    events = [
        {"kind": "registry", "record": {"number": 4, "task_id": tid, "title": "legacy", "workspace": str(project)}},
        {"kind": "claim", "task_id": unnumbered, "predicate": "task_status", "value": "COMPLETE"},  # no legacy number
    ]
    (project / ".maestro" / "project-state.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (home / "task-counter").write_text("10", encoding="utf-8")  # 1..10 were given out, then gc'd
    held = [0]
    locked_writes: list[bool] = []
    real_lock = Maestro._task_lock
    real_write = Maestro._write_registry_record

    @contextmanager
    def counting_lock(self):
        with real_lock(self):
            held[0] += 1
            try:
                yield
            finally:
                held[0] -= 1

    monkeypatch.setattr(Maestro, "_task_lock", counting_lock)
    monkeypatch.setattr(Maestro, "_write_registry_record", lambda self, record: locked_writes.append(held[0] > 0) or real_write(self, record))
    m = Maestro(project)
    try:
        assert [(x["task_id"], x["number"], x["legacy_task_number"]) for x in m._registry_records()] == [(tid, 11, 4), (unnumbered, 12, None)]
        assert locked_writes == [True, True]
        m.index_path.unlink()  # the rebuild keeps the legacy number too
        assert [(x["number"], x.get("legacy_task_number")) for x in m._registry_records()] == [(11, 4), (12, None)]
        m._write_claim("task-odd", "task_legacy_number", "not json")
        m.index_path.unlink()
        assert {x["task_id"]: x.get("legacy_task_number") for x in m._registry_records()}["task-odd"] == "not json"
    finally:
        m.close()


def test_the_parse_cache_notices_an_in_place_rewrite_with_the_same_size_and_mtime(home):
    state = _FileState(home)
    state.remember("s", "p", "a")
    assert [c.object for c in state.history("s", "p")] == ["a"]
    before = state.path.stat()
    text = state.path.read_text(encoding="utf-8").replace('"object": "a"', '"object": "b"')
    with state.path.open("r+", encoding="utf-8") as fh:  # same inode, same size
        fh.write(text)
    os.utime(state.path, ns=(before.st_atime_ns, before.st_mtime_ns))  # same mtime
    assert [c.object for c in state.history("s", "p")] == ["b"]


def test_the_parse_cache_checks_content_when_every_stat_field_collides(home, monkeypatch):
    # On ext4 an inode number is reused and timestamps can be coarse, so a new
    # journal of the same size can report exactly the same stat values.
    state = _FileState(home)
    state.remember("s", "p", "a")
    frozen = SimpleNamespace(st_ino=7, st_size=state.path.stat().st_size, st_mtime_ns=1, st_ctime_ns=1)
    real_stat = os.stat
    monkeypatch.setattr(core.os, "stat", lambda p, *a, **k: frozen if str(p) == str(state.path) else real_stat(p, *a, **k))
    monkeypatch.setattr(core.os, "fstat", lambda fd: frozen)
    assert [c.object for c in state.history("s", "p")] == ["a"]
    state.path.write_text(state.path.read_text(encoding="utf-8").replace('"object": "a"', '"object": "b"'), encoding="utf-8")
    assert [c.object for c in state.history("s", "p")] == ["b"]


def test_a_lock_that_cannot_be_taken_warns_once(tmp_path, monkeypatch, capsys):
    import fcntl

    monkeypatch.setattr(core, "_LOCK_WARNED", set(), raising=False)
    monkeypatch.setattr(fcntl, "flock", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOLCK, "No locks available")))
    lock = tmp_path / "state.lock"
    with core._file_lock(lock):
        pass
    with core._file_lock(lock):
        pass
    err = capsys.readouterr().err
    assert err.count(str(lock)) == 1
    assert "not locked" in err and "No locks available" in err
    monkeypatch.setitem(sys.modules, "fcntl", None)  # Windows: no fcntl module
    other = tmp_path / "registry.lock"
    with core._file_lock(other):
        pass
    assert str(other) in capsys.readouterr().err


def test_gc_skips_a_task_that_changed_after_it_was_chosen(home, tmp_path, monkeypatch, capsys):
    from maestro import cli

    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    _register(m, "task-old", "old", ws)
    m.close()
    task_dir = home / "tasks" / "task-old"
    task_dir.mkdir(parents=True)
    ancient = time.time() - 30 * 86400
    os.utime(task_dir, (ancient, ancient))
    real_unregister = Maestro.unregister_task

    def a_follow_up_starts_first(self, task_id, *a, **k):
        self._write_claim(task_id, "task_status", "WORKING")  # written after gc chose the task
        return real_unregister(self, task_id, *a, **k)

    monkeypatch.setattr(Maestro, "unregister_task", a_follow_up_starts_first)
    assert cli.main(["gc", "--days", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"removed": [], "kept": 1}
    assert task_dir.is_dir()
    m = Maestro(home)
    try:
        assert [x["task_id"] for x in m._registry_records()] == ["task-old"]
        assert m._claims("task-old")["task_status"] == "WORKING"
    finally:
        m.close()


def test_unregister_skips_when_the_check_under_the_lock_fails(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    try:
        _register(m, "task-a", "a", ws)
        seen: list[object] = []
        assert m.unregister_task("task-a", should_remove=lambda record: seen.append(record) or False) is None
        assert seen and seen[0]["task_id"] == "task-a"
        assert m.unregister_task("task-gone", should_remove=lambda record: record is not None) is None
        assert [x["task_id"] for x in m._registry_records()] == ["task-a"]
        assert m.unregister_task("task-a", should_remove=lambda record: True) == 4
        assert m._registry_records() == []
    finally:
        m.close()


def test_task_list_reports_an_unreadable_registry(home, tmp_path, monkeypatch, capsys):
    from maestro import cli

    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    _register(m, "task-a", "a", ws)
    m.close()
    monkeypatch.delenv("MAESTRO_WORKSPACE", raising=False)
    monkeypatch.delenv("MAESTRO_PROJECT", raising=False)
    monkeypatch.chdir(ws)
    real_read = Path.read_text

    def denied(self, *a, **k):
        if self.name == "registry.json":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", denied)
    assert cli.main(["task", "list"]) == 2
    assert "Could not read the task registry" in capsys.readouterr().err


def test_gc_skips_a_task_another_gc_removed_first(home, tmp_path, monkeypatch, capsys):
    from maestro import cli

    ws = tmp_path / "ws"
    ws.mkdir()
    m = Maestro(home)
    _register(m, "task-old", "old", ws)
    m.close()
    task_dir = home / "tasks" / "task-old"
    task_dir.mkdir(parents=True)
    ancient = time.time() - 30 * 86400
    os.utime(task_dir, (ancient, ancient))
    real_unregister = Maestro.unregister_task

    def another_gc_wins(self, task_id, *a, **k):
        real_unregister(self, task_id)  # a second `maestro gc` removed it first
        return real_unregister(self, task_id, *a, **k)

    monkeypatch.setattr(Maestro, "unregister_task", another_gc_wins)
    assert cli.main(["gc", "--days", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"removed": [], "kept": 1}
