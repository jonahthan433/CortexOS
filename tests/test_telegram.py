"""Phase 2 Telegram bridge tests: parsing, auth, allowlist, commands,
notifier dedupe/persistence, and queue dispatch pause."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from types import SimpleNamespace

import pytest

from cortexos.queue import QueueManager, QueueStore
from cortexos.telegram import (AllowlistCache, InboundMessage, TelegramBridge, TelegramNotifier,
                               ensure_bridge_token, is_trigger, parse_allowed_users, parse_command)


def run(coro):
    return asyncio.run(coro)


def make_settings(tmp_path, **overrides):
    base = dict(
        telegram_allow=("999",),
        telegram_env_file=tmp_path / "hermes.env",
        telegram_token_file=tmp_path / "telegram.token",
        telegram_rate_per_minute=20,
        telegram_max_reply_chars=3500,
        telegram_notify_retries=False,
        queue_db=tmp_path / "queue.db",
        agents=[{"id": "hermes", "name": "Hermes"}, {"id": "codex", "name": "Codex"}],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_manager(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    manager = QueueManager(SimpleNamespace(), store, workers=1)
    return manager, store


def make_bridge(tmp_path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    manager, store = make_manager(tmp_path)
    token_file = settings.telegram_token_file
    token_file.write_text("s3cret-token\n", encoding="utf-8")
    bridge = TelegramBridge(settings)
    bridge.bind(manager, store, SimpleNamespace(status=lambda: {"state": "Idle", "backend": "hermes", "skills": 42}))
    bridge.directory_provider = lambda: [
        {"id": "hermes", "name": "Hermes", "kind": "cli", "implemented": True},
        {"id": "research", "name": "Research", "kind": "role", "implemented": False},
    ]
    return bridge, manager, store, settings


def msg(text, chat="77", user="999", message_id=11):
    return InboundMessage(chat_id=chat, user_id=user, text=text, message_id=message_id, update_id=5)


# ------------------------------------------------------------------ parsing

def test_parse_command_variants():
    assert parse_command("/task fix the thing") == ("task", "fix the thing")
    assert parse_command("/cx task fix it") == ("task", "fix it")
    assert parse_command("/Task@CortexBot fix it") == ("task", "fix it")
    assert parse_command("/cx") == ("help", "")
    assert parse_command("/cx  ") == ("help", "")
    assert parse_command("/cx status") == ("status", "")
    assert parse_command("/taskstatus abc123") == ("taskstatus", "abc123")
    assert parse_command("hello") == (None, "hello")
    assert parse_command("/help") == ("help", "")


def test_trigger_battery():
    hit = [
        "/task do it", "/TASK DO IT", "/task@CortexBot do it", "/tasks", "/tasks@bot",
        "/agents", "/assign hermes x", "/taskstatus abc", "/cancel abc", "/reject abc",
        "/cx status", "/cx help", "/cx pause", "/cx",
        "/approve 123e4567-e89b-12d3-a456-426614174000",
        "/approve@CortexBot 123e4567-e89b-12d3-a456-426614174000",
    ]
    miss = [
        "hello", "/help", "/new", "/status", "/resume", "/pause", "/stop",
        "/approve", "/approve 42", "/queue", "/deny",
        "/taskstatusX", "/cxx", "/approve 123e4567",
    ]
    for text in hit:
        assert is_trigger(text), text
    for text in miss:
        assert not is_trigger(text), text


def test_parse_allowed_users_separators():
    assert parse_allowed_users("1,2;3 4") == {"1", "2", "3", "4"}
    assert parse_allowed_users("") == set()


# ------------------------------------------------------------------- auth

def test_allowlist_env_and_extra(tmp_path):
    env = tmp_path / "hermes.env"
    env.write_text("OTHER=1\nTELEGRAM_ALLOWED_USERS=111,222\n", encoding="utf-8")
    cache = AllowlistCache(env, extra=("333",))
    assert cache.allowed("111") and cache.allowed("222") and cache.allowed("333")
    assert not cache.allowed("444")
    assert not cache.allowed(None)
    env.write_text("TELEGRAM_ALLOWED_USERS=555\n", encoding="utf-8")
    os.utime(env, (time.time() + 1, time.time() + 1))
    cache.ttl = 0.0
    assert cache.allowed("555")


def test_token_check(tmp_path):
    bridge, *_ = make_bridge(tmp_path)
    assert bridge.token_ok("s3cret-token")
    assert not bridge.token_ok("wrong")
    assert not bridge.token_ok("")
    assert not bridge.token_ok(None)
    (tmp_path / "telegram.token").write_text("rotated\n", encoding="utf-8")
    os.utime(tmp_path / "telegram.token", (time.time() + 1, time.time() + 1))
    assert bridge.token_ok("rotated")
    assert not bridge.token_ok("s3cret-token")


def test_ensure_bridge_token_creates_restricted_file(tmp_path):
    path = tmp_path / "sub" / "telegram.token"
    assert ensure_bridge_token(path)
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o640
    content = path.read_text(encoding="utf-8").strip()
    assert len(content) >= 32
    assert ensure_bridge_token(path)  # idempotent


def test_rate_limit(tmp_path):
    bridge, *_ = make_bridge(tmp_path, telegram_rate_per_minute=3)
    assert bridge.rate_ok("999") and bridge.rate_ok("999") and bridge.rate_ok("999")
    assert not bridge.rate_ok("999")
    assert bridge.rate_ok("111")  # separate bucket


# ---------------------------------------------------------------- commands

def test_task_command_enqueues_with_dedupe(tmp_path):
    bridge, manager, store, _ = make_bridge(tmp_path)

    async def scenario():
        first = await bridge.handle(msg("/task investigate the outage"))
        second = await bridge.handle(msg("/task investigate the outage"))
        return first, second

    first, second = run(scenario())
    assert "queued" in first.lower()
    assert "Already queued" in second
    tasks = store.list(limit=10)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["origin"] == "telegram"
    assert task["dedupe_key"] == "tg:77:11"
    meta = task["meta"] if isinstance(task["meta"], dict) else json.loads(task["meta"])
    assert meta["telegram"]["chat_id"] == "77"
    assert meta["telegram"]["message_id"] == 11
    # A different message id is a different task.
    other = run(bridge.handle(msg("/task investigate the outage", message_id=12)))
    assert "Already" not in other
    assert len(store.list(limit=10)) == 2


def test_assign_resolves_agent_and_rejects_unknown(tmp_path):
    bridge, manager, store, _ = make_bridge(tmp_path)

    async def scenario():
        good = await bridge.handle(msg("/assign codex write the parser"))
        bad = await bridge.handle(msg("/assign nonexistent do work"))
        planned = await bridge.handle(msg("/assign research study this"))
        bare = await bridge.handle(msg("/assign codex"))
        return good, bad, planned, bare

    good, bad, planned, bare = run(scenario())
    assert "queued for codex" in good
    assert "Unknown agent" in bad and "hermes" in bad
    assert "not implemented" in planned
    assert "Usage" in bare
    task = [t for t in store.list(limit=10) if t["backend"] == "codex"][0]
    assert task["origin"] == "telegram"


def test_tasks_and_taskstatus_and_cancel(tmp_path):
    bridge, manager, store, _ = make_bridge(tmp_path)

    async def scenario():
        await bridge.handle(msg("/task clean the workshop"))
        listing = await bridge.handle(msg("/tasks"))
        task = store.list(limit=1)[0]
        status = await bridge.handle(msg(f"/taskstatus {task['id'][:8]}"))
        cancelled = await bridge.handle(msg(f"/cancel {task['id'][:8]}"))
        missing = await bridge.handle(msg("/cancel deadbeef"))
        return listing, status, cancelled, missing

    listing, status, cancelled, missing = run(scenario())
    assert "Recent tasks:" in listing and "clean the workshop" in listing
    assert "queued" in status and "Request: clean the workshop" in status
    assert "cancelled before it ran" in cancelled
    assert store.list(limit=1)[0]["status"] == "cancelled"
    assert "No task matches" in missing


def test_approve_reject_routing(tmp_path):
    bridge, manager, store, _ = make_bridge(tmp_path)
    calls = {"approve": [], "reject": []}
    bridge.approve_fn = lambda request_id: calls["approve"].append(request_id) or {"status": "running"}
    bridge.reject_fn = lambda request_id: calls["reject"].append(request_id) or {"status": "rejected"}

    approve_task, _ = store.enqueue("send the newsletter to the mailing list", origin="telegram", meta={})
    assert store.claim("wa")["id"] == approve_task["id"]
    store.await_approval(approve_task["id"], message="risk=external_action")
    reject_task, _ = store.enqueue("publish the report", origin="telegram", meta={})
    assert store.claim("wb")["id"] == reject_task["id"]
    store.await_approval(reject_task["id"], message="risk=external_action")
    plain_task, _ = store.enqueue("summarize the notes", origin="telegram", meta={})

    async def scenario():
        approve = await bridge.handle(msg(f"/cx approve {approve_task['id'][:8]}"))
        reject = await bridge.handle(msg(f"/reject {reject_task['id'][:8]}"))
        not_pending_ready = await bridge.handle(msg(f"/cx approve {plain_task['id'][:8]}"))
        return approve, reject, not_pending_ready

    approve, reject, not_pending_ready = run(scenario())
    assert "Approval accepted" in approve
    assert calls["approve"] == [approve_task["id"]]
    assert "rejected" in reject.lower()
    assert calls["reject"] == [reject_task["id"]]
    assert "not awaiting approval" in not_pending_ready


def test_status_pause_resume_and_help(tmp_path):
    bridge, manager, store, _ = make_bridge(tmp_path)

    async def scenario():
        status = await bridge.handle(msg("/cx status"))
        paused = await bridge.handle(msg("/cx pause"))
        paused_state = manager.dispatch_paused()
        status_paused = await bridge.handle(msg("/cx status"))
        resumed = await bridge.handle(msg("/cx resume"))
        resumed_state = manager.dispatch_paused()
        help_text = await bridge.handle(msg("/cx help"))
        return status, paused, paused_state, status_paused, resumed, resumed_state, help_text

    status, paused, paused_state, status_paused, resumed, resumed_state, help_text = run(scenario())
    assert "CortexOS status" in status and "Workers:" in status
    assert "Dispatch: running" in status
    assert "paused" in paused.lower()
    assert paused_state is True
    assert "paused" in status_paused
    assert "resumed" in resumed.lower()
    assert resumed_state is False
    assert "/task <text>" in help_text


def test_dispatch_pause_persists_and_blocks_claim(tmp_path):
    manager, store = make_manager(tmp_path)
    assert manager.dispatch_paused() is False
    manager.set_dispatch_paused(True)
    assert manager.dispatch_paused() is True
    second = QueueManager(SimpleNamespace(), QueueStore(tmp_path / "queue.db"), workers=1)
    assert second.dispatch_paused() is True  # same state dir
    manager.set_dispatch_paused(False)
    assert second.dispatch_paused() is False


# --------------------------------------------------------------- notifier

def test_notifier_completed_once_and_persistent(tmp_path):
    bridge, manager, store, settings = make_bridge(tmp_path)
    sent: list[tuple[str, str]] = []

    class Sender:
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

    notifier = TelegramNotifier(settings, store=store, sender=Sender(), sweep_seconds=999)
    task, _ = store.enqueue("summarize the logs", origin="telegram",
                            meta={"telegram": {"chat_id": "77", "message_id": 3}})
    store.claim("w1")
    store.complete(task["id"], result_ref="All clear. No errors in the last 24h.")

    async def scenario():
        await notifier.on_event(task["id"], {"type": "task"}, False)
        await notifier.on_event(task["id"], {"type": "task"}, False)

    run(scenario())
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "77"
    assert "completed" in text and "All clear" in text and task["id"][:8] in text

    # Restart: a fresh notifier must not re-send.
    fresh = TelegramNotifier(settings, store=store, sender=Sender(), sweep_seconds=999)

    async def again():
        await fresh.on_event(task["id"], {}, True)

    run(again())
    assert len(sent) == 1


def test_notifier_approval_and_failure_messages(tmp_path):
    bridge, manager, store, settings = make_bridge(tmp_path)
    sent: list[tuple[str, str]] = []

    class Sender:
        async def send(self, chat_id, text):
            sent.append((chat_id, text))

    notifier = TelegramNotifier(settings, store=store, sender=Sender(), sweep_seconds=999)
    meta = {"telegram": {"chat_id": "77", "message_id": 4}}
    pending, _ = store.enqueue("send the invoice email", origin="telegram", meta=meta)
    assert store.claim("w1")["id"] == pending["id"]
    store.await_approval(pending["id"], message="risk=external_action: sends external email")
    failed, _ = store.enqueue("build the report", origin="telegram", meta=meta)
    assert store.claim("w2")["id"] == failed["id"]
    store.fail(failed["id"], "provider quota exhausted", retryable=False)
    other, _ = store.enqueue("not telegram", origin="api", meta={})
    assert store.claim("w3")["id"] == other["id"]
    store.complete(other["id"], result_ref="nope")

    async def scenario():
        await notifier.on_event(pending["id"], {}, False)
        await notifier.on_event(failed["id"], {}, False)
        await notifier.on_event(other["id"], {}, False)

    run(scenario())
    assert len(sent) == 2
    approval_text = [t for c, t in sent if "approval" in t][0]
    assert f"/cx approve {pending['id'][:8]}" in approval_text
    assert f"/cx reject {pending['id'][:8]}" in approval_text
    failed_text = [t for c, t in sent if "failed" in t][0]
    assert "provider quota exhausted" in failed_text


def test_notifier_reads_vault_output_excerpt(tmp_path):
    bridge, manager, store, settings = make_bridge(tmp_path)
    sent: list[str] = []

    class Sender:
        async def send(self, chat_id, text):
            sent.append(text)

    notifier = TelegramNotifier(settings, store=store, sender=Sender(), sweep_seconds=999)
    output = tmp_path / "20261017-191140-output.md"
    output.write_text("# Skills\n\n477 skills registered, newest: morning-intel.\n", encoding="utf-8")
    task, _ = store.enqueue("list skills", origin="telegram", meta={"telegram": {"chat_id": "77"}})
    store.claim("w1")
    store.complete(task["id"], result_ref=str(output))

    async def scenario():
        await notifier.on_event(task["id"], {}, True)

    run(scenario())
    assert len(sent) == 1
    assert "477 skills registered" in sent[0]
    assert "Full result saved in the vault" in sent[0]


def test_notifier_uses_full_prompt_when_no_result(tmp_path):
    bridge, manager, store, settings = make_bridge(tmp_path)
    sent = []

    class Sender:
        async def send(self, chat_id, text):
            sent.append(text)

    notifier = TelegramNotifier(settings, store=store, sender=Sender(), sweep_seconds=999)
    task, _ = store.enqueue("noop", origin="telegram", meta={"telegram": {"chat_id": "77"}})
    store.claim("w1")
    store.complete(task["id"], result_ref=None)

    async def scenario():
        await notifier.on_event(task["id"], {}, True)

    run(scenario())
    assert len(sent) == 1 and "completed" in sent[0]
