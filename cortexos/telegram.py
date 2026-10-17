"""Telegram command-center bridge for CortexOS.

Architecture (see the completion ledger, Phase 2):

* **Ingress** — the existing Hermes gateway (which already owns the Telegram
  bot via long polling) forwards only explicit CortexOS commands to
  ``POST /api/telegram/inbound`` over loopback, guarded by a scoped bridge
  token file (never the main bridge token). No second poller is run and the
  existing Hermes conversation is untouched: the gateway plugin intercepts
  only its own trigger commands and stops propagation for exactly those.
* **Queue** — commands become durable tasks in the existing queue with
  ``origin="telegram"``, a ``dedupe_key`` of ``tg:<chat>:<message_id>``
  (Telegram redelivery can never double-execute) and the chat identity in
  ``meta.telegram`` for notifications and audit.
* **Egress** — progress/results go back to the same Telegram chat through
  ``hermes send`` (the bot's own credentials, no LLM, works even while the
  gateway restarts). The notifier dedupes per (task, stage) and persists, so
  restarts never re-notify.

Secrets: the bridge token is generated 0640 root:hermes in ``/etc/cortexos/``;
it is never logged, echoed, or returned in errors. The allowlist reuses the
Hermes ``TELEGRAM_ALLOWED_USERS`` variable (single source of truth) plus an
optional explicit list from ``[telegram] allow``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("cortexos.telegram")

BRIDGE_HEADER = "x-cortex-telegram-bridge"
DEFAULT_ENV_FILE = "/home/hermes/.hermes/.env"
DEFAULT_TOKEN_FILE = "/etc/cortexos/telegram-bridge.token"
DEFAULT_SEND_COMMAND = "/opt/cortexos/hermes-run"

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_VERB = r"(?:cx|task|tasks|agents|assign|taskstatus|cancel|reject)(?:@[A-Za-z0-9_]+)?"

# Kept in lockstep with integrations/hermes-telegram-bridge/__init__.py
# (parity-tested by that plugin's harness).
TRIGGER_RE = re.compile(
    rf"^/(?:{_VERB})(?:\s|$)|^/approve(?:@[A-Za-z0-9_]+)?\s+{_UUID_RE}\s*$",
    re.IGNORECASE,
)

STATUS_GLYPHS = {
    "queued": "○", "running": "▶", "awaiting_approval": "⏸",
    "completed": "✅", "failed": "❌", "cancelled": "🚫",
}


def is_trigger(text: str) -> bool:
    return bool(TRIGGER_RE.match((text or "").strip()))


def parse_allowed_users(raw: str) -> set[str]:
    """``TELEGRAM_ALLOWED_USERS`` may be comma/space/semicolon separated."""
    return {part.strip() for part in re.split(r"[,\s;]+", raw or "") if part.strip()}


def parse_command(text: str) -> tuple[str | None, str]:
    """Return ``(verb, rest)``.

    ``/task foo`` and ``/cx task foo`` both yield ``("task", "foo")``;
    ``/cx`` alone yields ``("help", "")``. Unknown verbs are returned as-is
    so the caller can decide (the trigger regex already narrows the plugin).
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None, stripped
    first, _, tail = stripped.partition(" ")
    first = first.split("@", 1)[0].lower()
    tail = tail.strip()
    if first == "/cx":
        sub, _, sub_rest = tail.partition(" ")
        sub = sub.strip().lower()
        if not sub:
            return "help", ""
        return sub, sub_rest.strip()
    return first[1:], tail


def short_id(task_id: str | None) -> str:
    return (task_id or "")[:8]


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _collapse(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


def ensure_bridge_token(path: Path | str) -> bool:
    """Create the scoped ingress token if missing. Returns True when a token
    file exists afterwards. Best-effort ``root:hermes`` 0640 ownership."""
    path = Path(path)
    if path.exists():
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
    except FileExistsError:
        return True
    except OSError:
        logger.exception("could not create telegram bridge token file")
        return False
    with contextlib.suppress(Exception):
        import grp

        os.chown(path, 0, grp.getgrnam("hermes").gr_gid)
    return True


class AllowlistCache:
    """Authorized Telegram user ids: explicit config list plus the Hermes
    ``TELEGRAM_ALLOWED_USERS`` env file (re-read when its mtime changes)."""

    def __init__(self, env_file: Path | str, extra: tuple[str, ...] = (), ttl: float = 5.0):
        self.env_file = Path(env_file)
        self.extra = {str(item) for item in extra if str(item).strip()}
        self.ttl = ttl
        self._cache: tuple[float, float, set[str]] | None = None  # (checked_at, mtime, ids)

    def _file_ids(self) -> set[str]:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.ttl:
            return self._cache[2]
        mtime = -1.0
        ids: set[str] = set()
        with contextlib.suppress(OSError):
            stat = self.env_file.stat()
            mtime = stat.st_mtime
            for line in self.env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("TELEGRAM_ALLOWED_USERS="):
                    ids |= parse_allowed_users(line.split("=", 1)[1])
                    break
        self._cache = (now, mtime, ids)
        return ids

    def allowed(self, user_id: str | int | None) -> bool:
        if user_id is None:
            return False
        candidate = str(user_id).strip()
        if not candidate:
            return False
        if candidate in self.extra:
            return True
        return candidate in self._file_ids()


@dataclass
class InboundMessage:
    chat_id: str
    user_id: str
    text: str
    message_id: int | None = None
    update_id: int | None = None
    username: str | None = None
    chat_type: str | None = None
    ts: float | None = None


class TelegramBridge:
    """Command handling for inbound Telegram messages. All state that can
    change after construction (queue manager, store, service, callbacks) is
    bound at startup so tests can inject fakes."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.manager: Any = None
        self.store: Any = None
        self.service: Any = None
        self.approve_fn: Callable[[str], dict] | None = None
        self.reject_fn: Callable[[str], Any] | None = None
        self.directory_provider: Callable[[], list[dict]] | None = None
        self._limit = max(1, int(getattr(settings, "telegram_rate_per_minute", 20) or 20))
        self._hits: dict[str, deque[float]] = {}
        self._token_cache: tuple[float, str] | None = None
        self._allow = AllowlistCache(
            Path(getattr(settings, "telegram_env_file", DEFAULT_ENV_FILE) or DEFAULT_ENV_FILE),
            tuple(getattr(settings, "telegram_allow", ()) or ()),
        )

    # ------------------------------------------------------------------ auth

    def _load_token(self) -> str:
        path = Path(getattr(self.settings, "telegram_token_file", DEFAULT_TOKEN_FILE))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        if self._token_cache and self._token_cache[0] == mtime:
            return self._token_cache[1]
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        self._token_cache = (mtime, token)
        return token

    def token_ok(self, supplied: str | None) -> bool:
        expected = self._load_token()
        if not expected or not supplied:
            return False
        return hmac.compare_digest(str(supplied).encode("utf-8"), expected.encode("utf-8"))

    def user_allowed(self, user_id: str | int | None) -> bool:
        return self._allow.allowed(user_id)

    def rate_ok(self, user_id: str | int) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault(str(user_id), deque())
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        return True

    def bind(self, manager: Any, store: Any, service: Any) -> None:
        self.manager = manager
        self.store = store
        self.service = service

    # --------------------------------------------------------------- helpers

    def _cap(self, text: str) -> str:
        limit = int(getattr(self.settings, "telegram_max_reply_chars", 3500) or 3500)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _resolve_task(self, prefix: str) -> dict | None:
        token = (prefix or "").strip()
        if not token:
            return None
        if self.store is None:
            return None
        with contextlib.suppress(Exception):
            exact = self.store.get(token) or self.store.find_by_request_id(token)
            if exact:
                return exact
        if len(token) >= 4:
            with contextlib.suppress(Exception):
                for task in self.store.list(limit=200):
                    if str(task.get("id", "")).lower().startswith(token.lower()):
                        return task
        return None

    def _agent_candidates(self) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        if self.directory_provider is not None:
            with contextlib.suppress(Exception):
                for entry in self.directory_provider() or []:
                    agent_id = str(entry.get("id") or "")
                    if not agent_id or agent_id in seen:
                        continue
                    seen.add(agent_id)
                    rows.append({"id": agent_id, "name": str(entry.get("name") or agent_id),
                                 "implemented": bool(entry.get("implemented", True)),
                                 "kind": str(entry.get("kind") or "")})
        for agent in (getattr(self.settings, "agents", None) or []):
            agent_id = str(agent.get("id") or "")
            if not agent_id or agent_id in seen:
                continue
            seen.add(agent_id)
            rows.append({"id": agent_id, "name": str(agent.get("name") or agent_id),
                         "implemented": True, "kind": "cli"})
        return rows

    def _resolve_agent(self, name: str) -> dict | None:
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        candidates = self._agent_candidates()
        for entry in candidates:
            if entry["id"].lower() == wanted:
                return entry
        for entry in candidates:
            if entry["name"].lower() == wanted:
                return entry
        return None

    async def _enqueue(self, msg: InboundMessage, prompt: str, backend: str | None = None) -> tuple[dict | None, bool]:
        dedupe_key = f"tg:{msg.chat_id}:{msg.message_id}" if msg.message_id is not None else None
        meta = {"telegram": {"chat_id": msg.chat_id, "message_id": msg.message_id,
                             "user_id": msg.user_id, "update_id": msg.update_id}}
        result = await self.manager.enqueue(prompt, backend=backend, origin="telegram",
                                            dedupe_key=dedupe_key, meta=meta)
        if isinstance(result, tuple):
            return result  # (task, created)
        task = result.get("task") if isinstance(result, dict) else None
        created = bool(result.get("created", False)) if isinstance(result, dict) else False
        return task, created

    # -------------------------------------------------------------- commands

    async def handle(self, msg: InboundMessage) -> str:
        verb, rest = parse_command(msg.text)
        try:
            return self._cap(await self._dispatch(verb, rest, msg))
        except Exception:
            logger.exception("telegram command %r failed", verb)
            return "The command failed before it could run. Nothing was queued. Check the bridge logs."

    async def _dispatch(self, verb: str | None, rest: str, msg: InboundMessage) -> str:
        if verb is None:
            return self.help_text()
        if verb == "task":
            return await self._cmd_task(msg, rest)
        if verb == "assign":
            return await self._cmd_assign(msg, rest)
        if verb == "tasks":
            return self._cmd_tasks()
        if verb == "taskstatus":
            return self._cmd_taskstatus(rest)
        if verb == "cancel":
            return await self._cmd_cancel(rest)
        if verb == "approve":
            return await self._cmd_approve(rest)
        if verb == "reject":
            return await self._cmd_reject(rest)
        if verb == "agents":
            return self._cmd_agents()
        if verb in {"status"}:
            return self._cmd_status()
        if verb == "pause":
            return self._cmd_pause(True)
        if verb == "resume":
            return self._cmd_pause(False)
        if verb == "help":
            return self.help_text()
        return self.help_text()

    def help_text(self) -> str:
        return (
            "CortexOS commands:\n"
            "/task <text> — queue work\n"
            "/assign <agent> <text> — queue work for a specific agent\n"
            "/tasks — recent tasks\n"
            "/taskstatus <id> — task details (short ids work)\n"
            "/cancel <id> — cancel a queued or running task\n"
            "/agents — agents and readiness\n"
            "/cx status — system + queue status\n"
            "/cx approve <id> / /cx reject <id> — decide an approval\n"
            "/cx pause / /cx resume — stop/start new task dispatch\n"
            "/cx help — this list"
        )

    async def _cmd_task(self, msg: InboundMessage, prompt: str) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            return "Usage: /task <what should be done>"
        task, created = await self._enqueue(msg, prompt)
        if task is None:
            return "The queue did not accept the task; nothing was queued."
        if not created:
            return f"Already queued as task {short_id(task.get('id'))} — duplicate message ignored."
        return (f"🧠 Task {short_id(task.get('id'))} queued.\n"
                f"I'll report the result in this chat. Status: /taskstatus {short_id(task.get('id'))}")

    async def _cmd_assign(self, msg: InboundMessage, rest: str) -> str:
        agent_name, _, prompt = (rest or "").strip().partition(" ")
        prompt = prompt.strip()
        if not agent_name or not prompt:
            ids = ", ".join(entry["id"] for entry in self._agent_candidates()) or "none"
            return f"Usage: /assign <agent> <what should be done>\nAgents: {ids}"
        agent = self._resolve_agent(agent_name)
        if agent is None:
            ids = ", ".join(entry["id"] for entry in self._agent_candidates()) or "none"
            return f"Unknown agent '{agent_name}'. Available: {ids}"
        if not agent.get("implemented", True):
            return f"Agent '{agent['id']}' is listed but not implemented yet, so it cannot run work. Nothing was queued."
        task, created = await self._enqueue(msg, prompt, backend=agent["id"])
        if task is None:
            return "The queue did not accept the task; nothing was queued."
        if not created:
            return f"Already queued as task {short_id(task.get('id'))} — duplicate message ignored."
        return (f"🧠 Task {short_id(task.get('id'))} queued for {agent['id']}.\n"
                f"Status: /taskstatus {short_id(task.get('id'))}")

    def _cmd_tasks(self) -> str:
        tasks = self.store.list(limit=8) if self.store is not None else []
        if not tasks:
            return "No tasks yet."
        now = time.time()
        lines = ["Recent tasks:"]
        for task in tasks:
            glyph = STATUS_GLYPHS.get(str(task.get("status")), "•")
            origin = " (tg)" if task.get("origin") == "telegram" else ""
            age = fmt_age(now - float(task.get("updated_at") or now))
            prompt = _collapse(str(task.get("prompt") or ""), 60)
            lines.append(f"{glyph} {short_id(task.get('id'))} {task.get('status')}{origin} · {age} ago · {prompt}")
        lines.append("Details: /taskstatus <id>")
        return "\n".join(lines)

    def _cmd_taskstatus(self, rest: str) -> str:
        task = self._resolve_task(rest)
        if task is None:
            return f"No task matches '{rest.strip()}'. Use /tasks to list recent tasks."
        now = time.time()
        meta = task.get("meta") or {}
        if isinstance(meta, str):
            with contextlib.suppress(ValueError):
                meta = json.loads(meta)
        tier = meta.get("tier") if isinstance(meta, dict) else None
        backend = meta.get("backend") if isinstance(meta, dict) else None
        lines = [f"{STATUS_GLYPHS.get(str(task.get('status')), '•')} Task {short_id(task.get('id'))} — {task.get('status')}"]
        lines.append(f"Created {fmt_age(now - float(task.get('created_at') or now))} ago · "
                     f"attempts {task.get('attempts')}/{task.get('max_attempts')}"
                     + (f" · tier {tier}" if tier else "") + (f" · agent {backend}" if backend else ""))
        lines.append(f"Request: {_collapse(str(task.get('prompt') or ''), 140)}")
        if task.get("error"):
            lines.append(f"Error: {_collapse(str(task.get('error')), 180)}")
        if task.get("status") == "completed" and task.get("result_ref"):
            lines.append("Result: " + _collapse(str(task.get("result_ref")), 400))
        if task.get("status") == "awaiting_approval":
            lines.append(f"Decide with: /cx approve {short_id(task.get('id'))} or /cx reject {short_id(task.get('id'))}")
        with contextlib.suppress(Exception):
            events = self.store.events(task["id"], limit=3) or []
            for event in events[-3:]:
                kind = event.get("kind") or event.get("type") or ""
                lines.append(f"· {kind}: {_collapse(str(event.get('message') or ''), 80)}")
        return "\n".join(lines)

    async def _cmd_cancel(self, rest: str) -> str:
        task = self._resolve_task(rest)
        if task is None:
            return f"No task matches '{rest.strip()}'."
        status = str(task.get("status"))
        if status in {"completed", "failed", "cancelled"}:
            return f"Task {short_id(task.get('id'))} is already {status}; nothing to cancel."
        outcome = (await self.manager.cancel(task["id"])) if self.manager is not None else {"id": task["id"], "status": "unknown"}
        new_status = str(outcome.get("status") or "")
        if new_status == "cancelled":
            return f"🚫 Task {short_id(task.get('id'))} cancelled before it ran."
        if new_status == "cancel_requested":
            return (f"⏹ Cancellation requested for task {short_id(task.get('id'))}. "
                    "The running agent stops when it next yields; I'll confirm in this chat.")
        return f"Could not cancel task {short_id(task.get('id'))} (status {new_status or status})."

    async def _cmd_approve(self, rest: str) -> str:
        task = self._resolve_task(rest)
        if task is None:
            return f"No task matches '{rest.strip()}'."
        if task.get("status") != "awaiting_approval":
            return f"Task {short_id(task.get('id'))} is not awaiting approval (status: {task.get('status')}). Nothing to approve."
        request_id = task.get("request_id") or task["id"]
        if self.approve_fn is None:
            return "Approval is unavailable in this process; use the HUD."
        try:
            self.approve_fn(request_id)
        except Exception as exc:
            return f"Approval failed to start: {type(exc).__name__}. Nothing was executed."
        return (f"▶ Approval accepted for task {short_id(task.get('id'))}. "
                "Execution is running in the background; the result will arrive in this chat.")

    async def _cmd_reject(self, rest: str) -> str:
        task = self._resolve_task(rest)
        if task is None:
            return f"No task matches '{rest.strip()}'."
        if task.get("status") != "awaiting_approval":
            return f"Task {short_id(task.get('id'))} is not awaiting approval (status: {task.get('status')})."
        request_id = task.get("request_id") or task["id"]
        if self.reject_fn is None:
            return "Rejection is unavailable in this process; use the HUD."
        result = self.reject_fn(request_id)
        if asyncio.iscoroutine(result):
            result = await result
        status = (result or {}).get("status") if isinstance(result, dict) else None
        if status == "rejected":
            return f"🚫 Task {short_id(task.get('id'))} rejected. The action was not executed."
        return f"Rejection reported status '{status or 'unknown'}' for task {short_id(task.get('id'))}."

    def _cmd_agents(self) -> str:
        entries = self._agent_candidates()
        if not entries:
            return "No agents are registered."
        ready = [e for e in entries if e.get("implemented", True)]
        planned = [e for e in entries if not e.get("implemented", True)]
        lines = [f"Agents ready to run ({len(ready)} of {len(entries)}):"]
        for entry in ready[:14]:
            lines.append(f"• {entry['id']} — {entry.get('kind') or 'agent'}")
        if planned:
            lines.append("Planned (cannot run yet): " + ", ".join(entry["id"] for entry in planned))
        lines.append("Assign with: /assign <agent> <task>")
        return "\n".join(lines)

    def _cmd_status(self) -> str:
        try:
            status = self.service.status() if self.service is not None else {}
        except Exception:
            status = {}
        stats = self.store.stats() if self.store is not None else {}
        workers = self.store.workers() if self.store is not None else []
        alive = sum(1 for worker in workers if worker.get("alive"))
        paused = bool(self.manager.dispatch_paused()) if self.manager is not None else False
        lines = [
            "🧠 CortexOS status",
            f"State: {status.get('state', 'unknown')} · default agent: {status.get('backend', '?')} · skills: {status.get('skills', '?')}",
            "Queue: " + " · ".join(
                f"{name} {stats.get(name, 0)}"
                for name in ("queued", "running", "awaiting_approval", "failed", "completed")
            ),
            f"Workers: {alive}/{len(workers)} alive",
            f"Dispatch: {'paused (new tasks wait)' if paused else 'running'}",
        ]
        return "\n".join(lines)

    def _cmd_pause(self, paused: bool) -> str:
        if self.manager is None:
            return "The queue manager is not available."
        self.manager.set_dispatch_paused(paused)
        if paused:
            return "⏸ Dispatch paused: queued tasks wait, running work continues."
        return "▶ Dispatch resumed: queued tasks will start now."


class HermesSendSender:
    """Outbound: the bot's own credentials through ``hermes send`` — no second
    poller, no LLM, works independently of the gateway process."""

    def __init__(self, command: str = DEFAULT_SEND_COMMAND, timeout: float = 60.0):
        self.command = str(command or DEFAULT_SEND_COMMAND)
        self.timeout = float(timeout or 60.0)

    async def send(self, chat_id: str, text: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            self.command, "send", "--to", f"telegram:{chat_id}", "--quiet", text,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            raise RuntimeError("hermes send timed out") from None
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "replace").strip()[-200:]
            raise RuntimeError(f"hermes send failed (exit {proc.returncode}){': ' + tail if tail else ''}")


class TelegramNotifier:
    """Sends task state transitions for ``origin="telegram"`` tasks back to
    the originating chat. One message per (task, stage), persisted so
    restarts and duplicate events never double-notify."""

    STAGES = ("approval", "completed", "failed", "cancelled", "rejected", "retry")

    def __init__(self, settings: Any, store: Any = None, sender: Any = None,
                 state_path: Path | None = None, sweep_seconds: float = 30.0):
        self.settings = settings
        self.store = store
        self.sender = sender
        state_dir = Path(getattr(settings, "queue_db", "state/queue.db")).parent
        self.state_path = Path(state_path) if state_path else state_dir / "telegram-notify.json"
        self.sweep_seconds = float(sweep_seconds)
        self.notify_retries = bool(getattr(settings, "telegram_notify_retries", False))
        self._notified: dict[str, list[str]] = self._load()
        self._lock = asyncio.Lock()
        self._last_send = 0.0

    # ------------------------------------------------------------ persistence

    def _load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): [str(v) for v in (vals or [])] for k, vals in data.items() if isinstance(vals, list)}
        except (OSError, ValueError):
            pass
        return {}

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._notified, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
            with contextlib.suppress(OSError):
                os.chmod(self.state_path, 0o600)

    def _mark(self, task_id: str, stage: str) -> None:
        stages = self._notified.setdefault(task_id, [])
        if stage not in stages:
            stages.append(stage)
        if len(self._notified) > 1000:
            for key in list(self._notified)[: len(self._notified) - 1000]:
                self._notified.pop(key, None)
        self._save()

    def _already(self, task_id: str, stage: str) -> bool:
        return stage in self._notified.get(task_id, [])

    # ---------------------------------------------------------------- stages

    def _stage_for(self, task: dict) -> str | None:
        status = str(task.get("status") or "")
        error = str(task.get("error") or "")
        if status == "awaiting_approval":
            return "approval"
        if status == "completed":
            return "completed"
        if status == "failed":
            return "failed"
        if status == "cancelled":
            return "rejected" if "approval rejected" in error else "cancelled"
        attempts = int(task.get("attempts") or 0)
        if status == "queued" and attempts >= 1 and self.notify_retries:
            return "retry"
        return None

    def _chat_id(self, task: dict) -> str | None:
        meta = task.get("meta") or {}
        if isinstance(meta, str):
            with contextlib.suppress(ValueError):
                meta = json.loads(meta)
        telegram_meta = meta.get("telegram") if isinstance(meta, dict) else None
        chat_id = (telegram_meta or {}).get("chat_id") if isinstance(telegram_meta, dict) else None
        return str(chat_id) if chat_id not in (None, "") else None

    def compose(self, task: dict, stage: str) -> str:
        sid = short_id(task.get("id"))
        if stage == "approval":
            meta = task.get("meta") or {}
            if isinstance(meta, str):
                with contextlib.suppress(ValueError):
                    meta = json.loads(meta)
            reason = _collapse(str((meta or {}).get("reason") or task.get("error") or "risk gate"), 200)
            return (f"⏸ Task {sid} needs your approval: {reason}\n"
                    f"Approve: /cx approve {sid}\nReject: /cx reject {sid}")
        if stage == "completed":
            excerpt = self._result_excerpt(task.get("result_ref"))
            body = f"✅ Task {sid} completed."
            if excerpt:
                body += f"\n{excerpt}"
                if str(task.get("result_ref") or "").startswith("/"):
                    body += "\n(Full result saved in the vault · view in the HUD.)"
            return body
        if stage == "failed":
            err = _collapse(str(task.get("error") or "unknown error"), 400)
            return (f"❌ Task {sid} failed after {task.get('attempts')}/{task.get('max_attempts')} attempts: {err}\n"
                    f"Details: /taskstatus {sid}")
        if stage == "cancelled":
            return f"🚫 Task {sid} cancelled."
        if stage == "rejected":
            return f"🚫 Task {sid} rejected — the action was not executed."
        if stage == "retry":
            err = _collapse(str(task.get("error") or "provider issue"), 200)
            return (f"⏳ Task {sid} hit an issue ({err}); retry {task.get('attempts')}/{task.get('max_attempts')} scheduled.")
        return f"Task {sid} update: {task.get('status')}"

    @staticmethod
    def _result_excerpt(result_ref: Any) -> str:
        """Completed tasks store a vault output path; read a short excerpt for
        the chat, falling back to the reference itself."""
        ref = str(result_ref or "").strip()
        if not ref:
            return ""
        if ref.startswith("/") and len(ref) < 500 and os.path.isfile(ref):
            with contextlib.suppress(OSError):
                with open(ref, "r", encoding="utf-8", errors="replace") as stream:
                    return _collapse(stream.read(4000), 1200)
        return _collapse(ref, 1200)

    async def _maybe_notify(self, task: dict) -> None:
        if not task or str(task.get("origin") or "") != "telegram":
            return
        stage = self._stage_for(task)
        if stage is None:
            return
        task_id = str(task.get("id") or "")
        chat_id = self._chat_id(task)
        if not task_id or not chat_id or self._already(task_id, stage):
            return
        if self.sender is None:
            return
        text = self.compose(task, stage)
        async with self._lock:
            delay = 1.2 - (time.monotonic() - self._last_send)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.sender.send(chat_id, text)
            except Exception:
                logger.exception("telegram notification failed for task %s (%s)", task_id, stage)
                return
            self._last_send = time.monotonic()
            self._mark(task_id, stage)

    # ----------------------------------------------------------------- entry

    async def on_event(self, stream_id: str, event: dict, finish: bool) -> None:
        try:
            if self.store is None:
                return
            task = self.store.get(stream_id) or self.store.find_by_request_id(stream_id)
            await self._maybe_notify(task)
        except Exception:
            logger.exception("telegram notifier event error")

    async def run(self) -> None:
        """Periodic sweep: catches notifications missed while a send failed or
        the process restarted between a state change and its event."""
        while True:
            await asyncio.sleep(self.sweep_seconds)
            try:
                if self.store is not None:
                    for task in self.store.list(limit=100):
                        await self._maybe_notify(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("telegram notifier sweep failed")
