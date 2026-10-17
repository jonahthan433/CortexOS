"""Stage 4 queue store tests: durability, dedupe, backoff, cancel, recovery."""
from __future__ import annotations

import time

import pytest

from cortexos.queue import QueueError, QueueStore


@pytest.fixture()
def store(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    yield store
    store.close()


def test_enqueue_get_and_validate(store):
    task, created = store.enqueue("do the thing", origin="test")
    assert created
    assert task["status"] == "queued"
    assert task["attempts"] == 0
    fetched = store.get(task["id"])
    assert fetched["prompt"] == "do the thing"
    assert fetched["origin"] == "test"
    with pytest.raises(QueueError):
        store.enqueue("bad priority", priority=0)
    with pytest.raises(QueueError):
        store.enqueue("bad attempts", max_attempts=99)


def test_dedupe_key_is_permanent_idempotency(store):
    first, created = store.enqueue("send report", dedupe_key="report:2026-10-17")
    assert created
    second, created2 = store.enqueue("send report", dedupe_key="report:2026-10-17")
    assert not created2 and second["id"] == first["id"]
    # Even after the first task reaches a terminal state the key stays taken.
    claimed = store.claim("w1")
    store.complete(claimed["id"])
    third, created3 = store.enqueue("send report", dedupe_key="report:2026-10-17")
    assert not created3 and third["id"] == first["id"]
    assert third["status"] == "completed"


def test_dedupe_window_applies_only_while_active(store):
    now = time.time()
    first, created = store.enqueue("same prompt", dedupe_window_seconds=30, now=now)
    assert created
    dup, created2 = store.enqueue("same prompt", dedupe_window_seconds=30, now=now + 5)
    assert not created2 and dup["id"] == first["id"]
    fresh, created3 = store.enqueue("same prompt", dedupe_window_seconds=30, now=now + 31)
    assert created3 and fresh["id"] != first["id"]


def test_claim_respects_priority_then_fifo(store):
    now = time.time()
    low, _ = store.enqueue("low", priority=9, now=now)
    early, _ = store.enqueue("early", priority=5, now=now + 1)
    late, _ = store.enqueue("late", priority=5, now=now + 2)
    first = store.claim("w1", now=now + 3)
    second = store.claim("w1", now=now + 3)
    third = store.claim("w1", now=now + 3)
    assert [first["id"], second["id"], third["id"]] == [early["id"], late["id"], low["id"]]
    assert store.claim("w1", now=now + 3) is None


def test_claim_attempts_and_request_ids(store):
    task, _ = store.enqueue("retry me", task_id="t-1")
    assert task["id"] == "t-1"
    first = store.claim("w1")
    assert first["attempts"] == 1
    assert first["request_id"] == "t-1"
    assert first["status"] == "running"
    failed = store.fail("t-1", "provider quota", retryable=True, backoff_base=10, backoff_cap=100)
    assert failed["status"] == "queued"
    assert failed["next_attempt_at"] > time.time()  # backoff scheduled in the future
    assert store.claim("w1") is None  # not due yet
    again = store.claim("w1", now=failed["next_attempt_at"] + 0.01)
    assert again["attempts"] == 2
    assert again["request_id"] == "t-1-a2"


def test_retries_are_bounded_and_fail_closed(store):
    store.enqueue("always fails", max_attempts=3, task_id="t-2")
    claimed = store.claim("w1")
    assert claimed["attempts"] == 1
    queued = store.fail("t-2", "boom", retryable=True, backoff_base=1, backoff_cap=2)
    assert queued["status"] == "queued"
    claimed = store.claim("w1", now=time.time() + 60)
    assert claimed["attempts"] == 2
    queued = store.fail("t-2", "boom", retryable=True, backoff_base=1, backoff_cap=2)
    assert queued["status"] == "queued"
    claimed = store.claim("w1", now=time.time() + 120)
    assert claimed["attempts"] == 3
    final = store.fail("t-2", "boom", retryable=True, backoff_base=1, backoff_cap=2)
    assert final["status"] == "failed"
    assert final["retryable"] is True
    # Even long after, an exhausted task never comes back on its own.
    assert store.claim("w1", now=time.time() + 86400) is None


def test_non_retryable_failure_is_terminal(store):
    store.enqueue("no retry", max_attempts=5, task_id="t-3")
    store.claim("w1")
    updated = store.fail("t-3", "agent not installed", retryable=False)
    assert updated["status"] == "failed"
    assert updated["retryable"] is False


def test_manual_retry_uses_new_request_id_cycle(store):
    store.enqueue("try again", task_id="t-4")
    store.claim("w1")
    store.fail("t-4", "nope", retryable=False)
    retried, ok = store.retry("t-4")
    assert ok and retried["status"] == "queued"
    assert retried["attempts"] == 0 and retried["cycle"] == 1
    claimed = store.claim("w1")
    assert claimed["attempts"] == 1
    assert claimed["request_id"] == "t-4-r1a1"
    # Retrying an active task is refused.
    _, ok2 = store.retry("t-4")
    assert not ok2


def test_cancel_queued_and_running(store):
    queued, _ = store.enqueue("cancel me")
    assert store.cancel_queued(queued["id"])["status"] == "cancelled"
    running, _ = store.enqueue("kill me")
    store.claim("w1")
    assert store.request_cancel(running["id"]) is True
    assert store.get(running["id"])["cancel_requested"] == 1
    assert store.mark_cancelled(running["id"])["status"] == "cancelled"
    # Cancelling a terminal task is a no-op.
    assert store.request_cancel(running["id"]) is False


def test_recover_requeues_or_fails(store):
    store.enqueue("interrupted", task_id="t-5", max_attempts=3)
    store.claim("w1")
    store.enqueue("exhausted", task_id="t-6", max_attempts=1)
    store.claim("w1")
    report = store.recover()
    assert report["requeued"] == 1 and report["failed"] == 1
    assert store.get("t-5")["status"] == "queued"
    assert store.get("t-6")["status"] == "failed"
    assert "attempts exhausted" in store.get("t-6")["error"]


def test_recover_retires_previous_boot_workers(store):
    store.worker_heartbeat("w1-old", boot_token="old", now=time.time())
    report = store.recover(boot_token="new")
    assert report["workers_removed"] >= 1
    assert store.workers() == []


def test_events_are_recorded_in_order_and_filterable(store):
    store.enqueue("event me", task_id="t-7")
    store.claim("w1")
    store.complete("t-7", result_ref="vault/output/x.md")
    events = store.events("t-7")
    kinds = [event["kind"] for event in events]
    assert kinds == ["enqueued", "claimed", "completed"]
    after = store.events("t-7", after_seq=events[0]["seq"])
    assert [event["kind"] for event in after] == ["claimed", "completed"]


def test_stats_and_worker_heartbeat(store):
    store.enqueue("one")
    store.worker_heartbeat("w1", status="idle", pid=1234, now=1000)
    workers = store.workers(now=1000)
    assert workers[0]["alive"] is True
    assert store.workers(now=2000)[0]["alive"] is False
    stats = store.stats(now=time.time())
    assert stats["queued"] == 1 and stats["total"] == 1


def test_prune_removes_old_terminal_tasks(store):
    task, _ = store.enqueue("old")
    store.claim("w1")
    store.complete(task["id"])
    finished = store.get(task["id"])["finished_at"]
    assert store.prune(retention_days=30, now=finished + 3600) == 0
    assert store.prune(retention_days=0.0001, now=finished + 60) == 1
    assert store.get(task["id"]) is None
    assert store.events(task["id"]) == []


def test_scheduled_for_is_not_ready_early(store):
    now = time.time()
    store.enqueue("later", scheduled_for=now + 100)
    assert store.claim("w1", now=now + 5) is None
    assert store.claim("w1", now=now + 101) is not None


def test_has_active_origin(store):
    task, _ = store.enqueue("job run", origin="job:daily")
    assert store.has_active_origin("job:daily") is True
    assert store.has_active_origin("job:other") is False
    store.claim("w1")
    store.complete(task["id"])
    assert store.has_active_origin("job:daily") is False
