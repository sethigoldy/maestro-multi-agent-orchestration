from __future__ import annotations

import threading
import time

from maestro.events import EventBus, TaskEvent


def test_publish_and_subscribe_all_types():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish(TaskEvent(task_id="t1", type="state", data={"state": "working"}))
    event = sub.get(timeout=1)
    assert event is not None and event.task_id == "t1" and event.type == "state"
    assert event.seq == 1


def test_subscribe_type_filter():
    bus = EventBus()
    sub = bus.subscribe("output")
    bus.publish(TaskEvent(task_id="t1", type="state"))
    bus.publish(TaskEvent(task_id="t1", type="output", data={"line": "hi"}))
    event = sub.get(timeout=1)
    assert event is not None and event.type == "output"
    assert sub.get(timeout=0.2) is None


def test_ring_buffer_catchup():
    bus = EventBus(ring_size=3)
    for i in range(5):
        bus.publish(TaskEvent(task_id="t1", type="state", data={"i": i}))
    sub = bus.subscribe()
    events = []
    while True:
        event = sub.get(timeout=0.2)
        if event is None:
            break
        events.append(event.data["i"])
    assert events == [2, 3, 4]


def test_wait_predicate_and_timeout():
    bus = EventBus()
    sub = bus.subscribe()

    def _later() -> None:
        time.sleep(0.1)
        bus.publish(TaskEvent(task_id="t1", type="state", data={"state": "noise"}))
        bus.publish(TaskEvent(task_id="t1", type="state", data={"state": "completed"}))

    thread = threading.Thread(target=_later)
    thread.start()
    start = time.monotonic()
    event = sub.wait(predicate=lambda e: e.data.get("state") == "completed", timeout=5)
    thread.join()
    assert event is not None and event.data["state"] == "completed"
    assert time.monotonic() - start < 4


def test_wait_timeout_returns_none():
    bus = EventBus()
    sub = bus.subscribe()
    assert sub.wait(timeout=0.2) is None


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe()
    sub.close()
    bus.publish(TaskEvent(task_id="t1", type="state"))
    assert sub.get(timeout=0.2) is None


def test_history_filters():
    bus = EventBus()
    bus.publish(TaskEvent(task_id="a", type="state"))
    bus.publish(TaskEvent(task_id="b", type="output"))
    assert [e.task_id for e in bus.history("a")] == ["a"]
    assert [e.type for e in bus.history(types=("output",))] == ["output"]


def test_wait_without_timeout_blocks_until_event():
    bus = EventBus()
    sub = bus.subscribe()

    def _later() -> None:
        time.sleep(0.1)
        bus.publish(TaskEvent(task_id="t1", type="state"))

    thread = threading.Thread(target=_later)
    thread.start()
    event = sub.wait()  # no timeout: blocks indefinitely until an event
    thread.join()
    assert event is not None


def test_wait_zero_timeout_returns_none():
    bus = EventBus()
    sub = bus.subscribe()
    assert sub.wait(timeout=0) is None


def test_double_close_is_safe():
    bus = EventBus()
    sub = bus.subscribe()
    sub.close()
    sub.close()  # second close: unsubscribe loop finds nothing and exits cleanly
    bus.publish(TaskEvent(task_id="t1", type="state"))


def test_typed_catchup_skips_nonmatching_ring_events():
    bus = EventBus()
    bus.publish(TaskEvent(task_id="a", type="state"))
    bus.publish(TaskEvent(task_id="b", type="state"))
    bus.publish(TaskEvent(task_id="c", type="output"))
    sub = bus.subscribe("output")
    event = sub.get(timeout=0.2)
    assert event is not None and event.task_id == "c"
    assert sub.get(timeout=0.2) is None
