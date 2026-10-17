"""CortexOS Telegram bridge plugin for the Hermes gateway.

Wiring: ``register(ctx)`` queues a Telegram handler factory. The gateway's
Telegram adapter invokes it with ``(native, adapter)`` on connect (and hoists
late-loaded handlers ahead of core handlers), so this plugin runs first and
claims ONLY its own trigger commands; PTB's first-match rule plus an explicit
``ApplicationHandlerStop`` keeps every other message/command on its existing
Hermes path.

The bridge call itself is a loopback HTTPS POST to CortexOS guarded by a
scoped token file (default ``/etc/cortexos/telegram-bridge.token``); the reply
text is sent straight back through the same bot. No second poller, no LLM, no
credentials of our own.

Kept in lockstep with ``cortexos/telegram.py`` (TRIGGER_RE / is_trigger);
the offline harness asserts both agree on a shared battery of strings.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(os.environ.get("CORTEXOS_TELEGRAM_TOKEN_FILE",
                                 "/etc/cortexos/telegram-bridge.token"))
BRIDGE_URL = os.environ.get("CORTEXOS_TELEGRAM_BRIDGE_URL",
                            "https://127.0.0.1:8765/api/telegram/inbound")

# Same pattern as cortexos/telegram.py TRIGGER_RE — parity-tested.
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_VERB = r"(?:cx|task|tasks|agents|assign|taskstatus|cancel|reject)(?:@[A-Za-z0-9_]+)?"
TRIGGER_RE = re.compile(
    rf"^/(?:{_VERB})(?:\s|$)|^/approve(?:@[A-Za-z0-9_]+)?\s+{_UUID_RE}\s*$",
    re.IGNORECASE,
)


def _allowed_raw() -> str:
    """Same source of truth as Hermes core authz: TELEGRAM_ALLOWED_USERS.

    Prefers the core helper (profile-scoped env read), then the process env,
    then the .env file Hermes loads at boot. Empty means fail-closed.
    """
    try:
        from gateway.platforms._shared import platform_gate_env
        raw = platform_gate_env("TELEGRAM_ALLOWED_USERS")
        if raw:
            return raw
    except Exception:
        pass
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    if raw:
        return raw
    candidates = []
    home = os.environ.get("HERMES_HOME")
    if home:
        candidates.append(Path(home) / ".env")
    candidates.append(Path.home() / ".hermes" / ".env")
    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("TELEGRAM_ALLOWED_USERS="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return ""


def _allowed_ids() -> set[str]:
    return {part.strip() for part in re.split(r"[,\s;]+", _allowed_raw()) if part.strip()}


def _bridge_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def _post(payload: dict) -> str:
    """Loopback call to CortexOS. Never logs or echoes the token."""
    import aiohttp

    token = _bridge_token()
    if not token:
        raise RuntimeError("bridge token file is missing")
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Loopback-only TLS (self-signed): no verification needed for 127.0.0.1.
        async with session.post(BRIDGE_URL, json=payload, ssl=False,
                                headers={"X-Cortex-Telegram-Bridge": token}) as response:
            try:
                body = await response.json(content_type=None)
            except Exception:
                body = {}
            if response.status == 200 and isinstance(body, dict) and body.get("reply"):
                return str(body["reply"])
            if response.status == 403:
                return "This Telegram account is not authorized for CortexOS commands."
            if response.status == 429:
                return "Too many CortexOS commands right now — try again in a minute."
            if response.status == 503:
                return "CortexOS is not accepting Telegram commands right now (bridge disabled)."
            detail = body.get("detail") if isinstance(body, dict) else None
            raise RuntimeError(f"bridge HTTP {response.status}"
                               f"{': ' + str(detail) if detail else ''}")


async def _send(native, chat_id, text: str, reply_to) -> None:
    await native.bot.send_message(chat_id=chat_id, text=text[:4000],
                                  reply_to_message_id=reply_to)


async def _handle_update(update, context, native) -> None:
    message = getattr(update, "effective_message", None)
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    text = (message.text or "").strip() if message is not None and message.text else ""
    if not text or not TRIGGER_RE.match(text):
        return

    if user is None or str(user.id) not in _allowed_ids():
        # Consumed by first-match, no stop: the sender sees nothing, which is
        # the same silence Hermes core gives unauthorized senders.
        logger.warning("cortexos-bridge: ignored command from unauthorized sender")
        return

    payload = {
        "chat_id": str(chat.id),
        "user_id": str(user.id),
        "text": text,
        "message_id": message.message_id,
        "update_id": getattr(update, "update_id", None),
        "username": getattr(user, "username", None),
        "chat_type": getattr(chat, "type", None),
        "ts": message.date.timestamp() if message.date else None,
    }
    try:
        reply = await _post(payload)
    except Exception as exc:
        logger.exception("cortexos-bridge: CortexOS call failed")
        reply = (f"CortexOS bridge is unreachable right now ({type(exc).__name__}). "
                 "Nothing was queued.")
    if reply:
        try:
            await _send(native, chat.id, reply, message.message_id)
        except Exception:
            logger.exception("cortexos-bridge: reply delivery failed")

    from telegram.ext import ApplicationHandlerStop

    raise ApplicationHandlerStop


def _factory(native, adapter) -> None:
    """Invoked by the gateway adapter with the live PTB application. Wires this
    plugin's single message handler ahead of core handlers (same group, wired
    first or hoisted)."""
    from telegram.ext import MessageHandler, filters

    async def handler(update, context):
        await _handle_update(update, context, native)

    native.add_handler(MessageHandler(filters.TEXT & filters.Regex(TRIGGER_RE), handler),
                       group=0)
    logger.info("cortexos-bridge: telegram handlers wired")


def register(ctx) -> None:
    ctx.register_telegram_handler(_factory)
