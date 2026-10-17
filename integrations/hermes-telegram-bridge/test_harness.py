#!/usr/bin/env python3
"""Offline harness for the CortexOS telegram plugin.

Exercises the plugin through a REAL python-telegram-bot Application (handler
groups, first-match dispatch, ApplicationHandlerStop) with the plugin's
network boundaries monkeypatched (``_post``/``_send``). Nothing leaves the
machine and no bot token is used beyond a syntactically valid placeholder.

Run on the gateway host with the Hermes runtime (has PTB):

    CORTEXOS_SRC=/opt/cortexos/app \
    /home/hermes/.hermes/hermes-agent/venv/bin/python test_harness.py

Exit code 0 = all checks pass. Also asserts the plugin's TRIGGER_RE stays in
lockstep with ``cortexos/telegram.py`` (is_trigger) over a shared battery.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _core_module_path() -> Path:
    env = os.environ.get("CORTEXOS_SRC")
    if env:
        candidate = Path(env) / "cortexos" / "telegram.py"
        if candidate.exists():
            return candidate
    return HERE.parents[1] / "cortexos" / "telegram.py"


def payload(update_id: int, user_id: int, text: str, message_id: int = 100) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1761000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }


async def run_harness() -> int:
    os.environ["TELEGRAM_ALLOWED_USERS"] = "111,222"

    plugin = _load("cortexos_bridge_plugin", HERE / "__init__.py")
    core_path = _core_module_path()
    if not core_path.exists():
        print(f"FATAL: cortexos/telegram.py not found at {core_path} (set CORTEXOS_SRC)")
        return 2
    core = _load("cortexos_telegram_core", core_path)

    from telegram import Bot, Update, User
    from telegram.ext import Application, ApplicationHandlerStop, MessageHandler, filters

    # Bot.initialize() verifies the token with get_me(); stub ONLY that call so
    # the real Application lifecycle (initialize/process_update/shutdown) runs offline.
    async def _offline_get_me(self, *args, **kwargs):
        return User(id=1, is_bot=True, first_name="HarnessBot", username="harness_bot")

    Bot.get_me = _offline_get_me

    posted: list[dict] = []
    sent: list[dict] = []
    core_calls: list[int] = []

    async def fake_post(p: dict) -> str:
        posted.append(p)
        return "REPLY-FROM-CORTEXOS"

    async def fake_send(native, chat_id, text, reply_to) -> None:
        sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to})

    plugin._post = fake_post
    plugin._send = fake_send

    application = Application.builder().token("123456:TESTTOKEN").build()

    # Mirror gateway connect order: plugin factory wires first, core handlers after.
    plugin._factory(application, object())

    async def core_spy(update, context):
        core_calls.append(update.update_id)

    application.add_handler(MessageHandler(filters.ALL, core_spy), group=0)

    async def dispatch(update_payload: dict) -> None:
        update = Update.de_json(update_payload, application.bot)
        try:
            await application.process_update(update)
        except ApplicationHandlerStop:
            pass

    def reset():
        posted.clear()
        sent.clear()
        core_calls.clear()

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    await application.initialize()

    # 1. Authorized /task: forwarded once, replied to, core untouched.
    reset()
    await dispatch(payload(1, 111, "/task restart the printer", message_id=100))
    check("1a /task forwarded exactly once", len(posted) == 1, f"posted={len(posted)}")
    if posted:
        p = posted[0]
        check("1b payload fields complete",
              p.get("chat_id") == "111" and p.get("user_id") == "111"
              and p.get("message_id") == 100 and p.get("update_id") == 1
              and p.get("text") == "/task restart the printer"
              and p.get("chat_type") == "private",
              str({k: p.get(k) for k in ("chat_id", "user_id", "message_id", "update_id")}))
    check("1c reply sent with reply_to",
          len(sent) == 1 and sent[0]["text"] == "REPLY-FROM-CORTEXOS" and sent[0]["reply_to"] == 100,
          str(sent))
    check("1d core never saw it", core_calls == [], str(core_calls))

    # 2. /cx form and @bot suffix both forward.
    reset()
    await dispatch(payload(2, 111, "/cx status", message_id=101))
    await dispatch(payload(3, 111, "/task@CortexBot clean the desk", message_id=102))
    check("2a /cx status forwarded", len(posted) == 2 and posted[0]["text"] == "/cx status",
          str([p["text"] for p in posted]))
    check("2b @bot suffix forwarded", len(posted) == 2 and posted[1]["text"] == "/task@CortexBot clean the desk")
    check("2c core untouched", core_calls == [])

    # 3. /approve <uuid> is ours; /approve without uuid is Hermes'.
    reset()
    await dispatch(payload(4, 111, "/approve 123e4567-e89b-12d3-a456-426614174000", message_id=103))
    check("3a /approve uuid forwarded", len(posted) == 1 and posted[0]["text"].startswith("/approve 123e4567"))
    check("3b core untouched for uuid approve", core_calls == [])
    reset()
    await dispatch(payload(5, 111, "/approve", message_id=104))
    await dispatch(payload(6, 111, "/approve 42", message_id=105))
    check("3c bare /approve reaches Hermes core", sorted(core_calls) == [5, 6], str(core_calls))
    check("3d bare /approve never forwarded", posted == [])

    # 4. Unrelated commands/text keep flowing to Hermes.
    reset()
    await dispatch(payload(7, 111, "/new", message_id=106))
    await dispatch(payload(8, 111, "/help", message_id=107))
    await dispatch(payload(9, 111, "hello there", message_id=108))
    check("4a unrelated messages reach core", sorted(core_calls) == [7, 8, 9], str(core_calls))
    check("4b unrelated messages never forwarded", posted == [])

    # 5. Unauthorized sender: consumed silently (no forward, no reply, core untouched).
    reset()
    await dispatch(payload(10, 999, "/task wipe the disk", message_id=109))
    check("5a unauthorized not forwarded", posted == [], str(posted))
    check("5b unauthorized got no reply", sent == [], str(sent))
    check("5c unauthorized stays silent to core too", core_calls == [], str(core_calls))

    # 6. Bridge failure: honest error reply, still stops core.
    reset()
    async def failing_post(p: dict) -> str:
        raise RuntimeError("connection refused")
    plugin._post = failing_post
    await dispatch(payload(11, 111, "/tasks", message_id=110))
    plugin._post = fake_post
    check("6a failure reply is honest",
          len(sent) == 1 and "unreachable" in sent[0]["text"] and "Nothing was queued" in sent[0]["text"],
          str(sent))
    check("6b failure still stops core", core_calls == [], str(core_calls))

    # 7. Trigger-parity battery against cortexos/telegram.py.
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
    parity_ok = True
    for text in hit + miss:
        plugin_hit = bool(plugin.TRIGGER_RE.match(text))
        core_hit = bool(core.is_trigger(text))
        if plugin_hit != core_hit:
            parity_ok = False
            check(f"parity mismatch for {text!r}", False, f"plugin={plugin_hit} core={core_hit}")
    check("7a trigger parity with cortexos/telegram.py", parity_ok)
    for text in hit:
        check(f"7b battery hit {text!r}", bool(core.is_trigger(text)))
    for text in miss:
        check(f"7c battery miss {text!r}", not core.is_trigger(text))

    # 8. Allowlist fallback: process env cleared -> HERMES_HOME/.env is read.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text("TELEGRAM_ALLOWED_USERS=333,444\n", encoding="utf-8")
        saved_env = os.environ.pop("TELEGRAM_ALLOWED_USERS", None)
        saved_home = os.environ.pop("HERMES_HOME", None)
        os.environ["HERMES_HOME"] = tmp
        try:
            ids = plugin._allowed_ids()
            check("8a .env fallback reads the allowlist", ids == {"333", "444"}, str(ids))
        finally:
            if saved_env is not None:
                os.environ["TELEGRAM_ALLOWED_USERS"] = saved_env
            if saved_home is not None:
                os.environ["HERMES_HOME"] = saved_home
            else:
                os.environ.pop("HERMES_HOME", None)

    await application.shutdown()

    failures = [name for name, ok, _ in results if not ok]
    for name, ok, detail in results:
        status = "ok  " if ok else "FAIL"
        print(f"[{status}] {name}" + (f"   ({detail})" if detail and not ok else ""))
    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_harness()))
