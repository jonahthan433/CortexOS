"""Stage 4 queue manager tests: execution, retry, cancel, shutdown, recovery."""
from __future__ import annotations

import asyncio
import time

from cortexos.providers import ProviderError
from cortexos.queue import QueueManager, QueueStore
from cortexos.vault import safe_name


def run(coro):
    return asyncio.run(coro)


async def wait_for_status(store, task_id, status, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = store.get(task_id)
        if task and task["status"] == status:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {status}: {store.get(task_id)}")


class FakeService:
    def __init__(self, submit_impl, root):
        self.vault = type("Vault", (), {"root": root})()
        self.pending: dict = {}
        self._submit_impl = submit_impl

    async def submit(self, prompt, backend=None, skill_id=None, request_id=None, on_output=None):
        return await self._submit_impl(prompt, backend, skill_id, request_id, on_output)

    def reject(self, request_id):
        self.pending.pop(request_id, None)
        return {"id": request_id, "status": "rejected"}


def make_manager(tmp_path, submit_impl, *, workers=1):
    store = QueueStore(tmp_path / "queue.db")
    events: list = []

    async def sink(stream_id, event, finish):
        events.append({"stream": stream_id, "event": event, "finish": finish})

    service = FakeService(submit_impl, tmp_path)
    manager = QueueManager(service, store, workers=workers, poll_interval=0.05,
                           task_timeout=10, backoff_base=1, backoff_cap=2, event_sink=sink)
    return manager, store, events, service


def completed_impl(output="vault/output/x.md"):
    async def impl(prompt, backend, skill_id, request_id, on_output):
        if on_output:
            await on_output("working...\n")
        return {"id": request_id, "status": "completed", "output": output, "tier": 1,
                "backend": "local", "skill": None, "route_reason": "test", "receipt": None}
    return impl


def test_manager_executes_task_end_to_end(tmp_path):
    async def scenario():
        manager, store, events, service = make_manager(tmp_path, completed_impl())
        await manager.start()
        try:
            outcome = await manager.enqueue("list skills", origin="test")
            task_id = outcome["task"]["id"]
            task = await wait_for_status(store, task_id, "completed")
            assert task["result_ref"] == "vault/output/x.md"
            assert task["attempts"] == 1
            assert task["request_id"] == task_id  # first attempt uses the task id
            assert any(e["event"].get("data") == "working...\n" for e in events)
            finishes = [e for e in events if e["finish"]]
            assert finishes and finishes[-1]["event"]["status"] == "completed"
            assert finishes[-1]["stream"] == task_id
        finally:
            await manager.stop()
    run(scenario())


def test_manager_retries_transient_failure_then_succeeds(tmp_path):
    calls = {"n": 0}

    async def flaky(prompt, backend, skill_id, request_id, on_output):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": request_id, "status": "error", "error": "provider timeout", "retryable": True}
        return {"id": request_id, "status": "completed", "output": "vault/output/ok.md"}

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, flaky)
        await manager.start()
        try:
            outcome = await manager.enqueue("flaky job")
            task_id = outcome["task"]["id"]
            task = await wait_for_status(store, task_id, "completed")
            assert task["attempts"] == 2
            assert task["request_id"] == f"{task_id}-a2"
            kinds = [event["kind"] for event in store.events(task_id)]
            assert "retry_scheduled" in kinds and "completed" in kinds
        finally:
            await manager.stop()
    run(scenario())


def test_manager_fails_non_retryable_and_manual_retry(tmp_path):
    calls = {"n": 0}

    async def impl(prompt, backend, skill_id, request_id, on_output):
        calls["n"] += 1
        if calls["n"] <= 1:
            return {"id": request_id, "status": "error", "error": "hermes was not found",
                    "retryable": False}
        return {"id": request_id, "status": "completed", "output": "vault/output/second.md"}

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, impl)
        await manager.start()
        try:
            outcome = await manager.enqueue("bad job")
            task_id = outcome["task"]["id"]
            failed = await wait_for_status(store, task_id, "failed")
            assert failed["retryable"] is False
            assert calls["n"] == 1  # no automatic retry for non-retryable errors
            result = await manager.retry(task_id)
            assert result["status"] == "queued"
            done = await wait_for_status(store, task_id, "completed")
            assert done["cycle"] == 1
            assert done["request_id"] == f"{task_id}-r1a1"
        finally:
            await manager.stop()
    run(scenario())


def test_manager_cancels_running_task_cooperatively(tmp_path):
    async def hanging(prompt, backend, skill_id, request_id, on_output):
        await asyncio.Event().wait()

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, hanging)
        await manager.start()
        try:
            outcome = await manager.enqueue("hang forever")
            task_id = outcome["task"]["id"]
            await wait_for_status(store, task_id, "running")
            result = await manager.cancel(task_id)
            assert result["status"] == "cancel_requested"
            task = await wait_for_status(store, task_id, "cancelled")
            assert task["cancel_requested"] == 1
            assert "cancelled by human" in (task["error"] or "")
        finally:
            await manager.stop()
    run(scenario())


def test_manager_cancel_awaiting_approval_clears_pending(tmp_path):
    async def awaiting(prompt, backend, skill_id, request_id, on_output):
        return {"id": request_id, "status": "awaiting_approval", "risk_level": "high",
                "reason": "external action", "route_reason": "test"}

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, awaiting)
        await manager.start()
        try:
            outcome = await manager.enqueue("send email to team")
            task_id = outcome["task"]["id"]
            task = await wait_for_status(store, task_id, "awaiting_approval")
            assert task["meta"]["risk_level"] == "high"
            service.pending[task["request_id"]] = {"prompt": "send email to team"}
            result = await manager.cancel(task_id)
            assert result["status"] == "cancelled"
            assert service.pending == {}  # reject() cleared the live pending record
            assert store.get(task_id)["status"] == "cancelled"
        finally:
            await manager.stop()
    run(scenario())


def test_manager_note_rejection_and_approval_outcome(tmp_path):
    async def awaiting(prompt, backend, skill_id, request_id, on_output):
        return {"id": request_id, "status": "awaiting_approval", "risk_level": "high",
                "reason": "external action", "route_reason": "test"}

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, awaiting)
        await manager.start()
        try:
            outcome = await manager.enqueue("approval path")
            task_id = outcome["task"]["id"]
            task = await wait_for_status(store, task_id, "awaiting_approval")
            request_id = task["request_id"]
            await manager.note_rejection(request_id)
            assert store.get(task_id)["status"] == "cancelled"

            outcome = await manager.enqueue("approval path 2")
            task_id2 = outcome["task"]["id"]
            task2 = await wait_for_status(store, task_id2, "awaiting_approval")
            await manager.note_approval_outcome(task2["request_id"], {
                "id": task2["request_id"], "status": "completed", "output": "vault/output/approved.md"})
            after = store.get(task_id2)
            assert after["status"] == "completed"
            assert after["result_ref"] == "vault/output/approved.md"
        finally:
            await manager.stop()
    run(scenario())


def test_shutdown_requeues_running_task(tmp_path):
    async def hanging(prompt, backend, skill_id, request_id, on_output):
        await asyncio.Event().wait()

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, hanging)
        await manager.start()
        outcome = await manager.enqueue("slow job")
        task_id = outcome["task"]["id"]
        await wait_for_status(store, task_id, "running")
        await manager.stop()

        reopened = QueueStore(tmp_path / "queue.db")
        try:
            task = reopened.get(task_id)
            assert task is not None and task["status"] == "queued"
            assert task["attempts"] == 1  # the interrupted attempt still counts
            assert any(e["kind"] == "interrupted_by_shutdown" for e in reopened.events(task_id))
        finally:
            reopened.close()
    run(scenario())


def test_reconcile_awaiting_states_after_restart(tmp_path):
    async def awaiting(prompt, backend, skill_id, request_id, on_output):
        return {"id": request_id, "status": "awaiting_approval", "risk_level": "high",
                "reason": "external action", "route_reason": "test"}

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, awaiting)
        await manager.start()
        ids = []
        for prompt in ("completed case", "failed case", "rejected case"):
            outcome = await manager.enqueue(prompt)
            task = await wait_for_status(store, outcome["task"]["id"], "awaiting_approval")
            ids.append((task["id"], task["request_id"]))
        await manager.stop()

        completed_id, completed_rid = ids[0]
        (tmp_path / "output").mkdir(exist_ok=True)
        (tmp_path / "output" / f"20261017-120000-{completed_rid}.md").write_text("# done", encoding="utf-8")
        (tmp_path / "requests").mkdir(exist_ok=True)
        rejected_id, rejected_rid = ids[2]
        (tmp_path / "requests" / f"{safe_name(rejected_rid)}.md").write_text(
            f"# Request {rejected_rid}\n\nStatus: `rejected`\n", encoding="utf-8")

        store2 = QueueStore(tmp_path / "queue.db")
        service2 = FakeService(awaiting, tmp_path)
        manager2 = QueueManager(service2, store2, workers=1, poll_interval=0.05, task_timeout=10)
        await manager2.start()
        try:
            assert store2.get(completed_id)["status"] == "completed"
            assert store2.get(completed_id)["result_ref"].endswith(f"-{completed_rid}.md")
            assert store2.get(rejected_id)["status"] == "cancelled"
            failed_id = ids[1][0]
            failed = store2.get(failed_id)
            assert failed["status"] == "failed"
            assert "approval decision missing" in failed["error"]
        finally:
            await manager2.stop()
    run(scenario())


def test_reconcile_keeps_live_pending(tmp_path):
    async def awaiting(prompt, backend, skill_id, request_id, on_output):
        return {"id": request_id, "status": "awaiting_approval", "risk_level": "high",
                "reason": "external action", "route_reason": "test"}

    async def scenario():
        manager, store, events, service = make_manager(tmp_path, awaiting)
        await manager.start()
        outcome = await manager.enqueue("still pending")
        task = await wait_for_status(store, outcome["task"]["id"], "awaiting_approval")
        service.pending[task["request_id"]] = {"prompt": "still pending"}
        await manager.stop()

        store2 = QueueStore(tmp_path / "queue.db")
        service2 = FakeService(awaiting, tmp_path)
        service2.pending[task["request_id"]] = {"prompt": "still pending"}
        manager2 = QueueManager(service2, store2, workers=1, poll_interval=0.05, task_timeout=10)
        await manager2.start()
        try:
            assert store2.get(task["id"])["status"] == "awaiting_approval"
        finally:
            await manager2.stop()
    run(scenario())


def test_provider_error_retryable_flag():
    assert ProviderError("missing").retryable is False
    assert ProviderError("timeout", retryable=True).retryable is True
