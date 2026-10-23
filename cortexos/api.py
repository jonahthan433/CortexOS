from __future__ import annotations

import base64
import binascii
import asyncio
import logging
import time
import uuid
import hmac

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated, Any, Literal

from .config import Settings
from .service import CortexService
from .providers import ProviderError
from .browse import VaultBrowser, BrowseError, agents_directory, search_activity
from .sessions import TerminalManager, terminal_websocket
from .streaming import RequestStreams, MetricsBroadcaster
from .scheduler import run_scheduler
from .queue import QueueManager, QueueStore, QueueError
from .connectors import (ConnectorApprovalRequired, ConnectorBlocked, ConnectorError,
                         ConnectorRateLimited, ConnectorValidationError, scrub_secrets)
from .agents import AgentCoordinator
from .ops import HealthCheck, BackupManager, backup_loop
from .voice import LocalVoice, VoiceUnavailable
from .auth import (AuthError, AuthStore, LoginThrottle, MIN_PASSWORD_LENGTH,
                   SESSION_COOKIE, SESSION_TTL_SECONDS)
from . import telegram as telegram_module
from .telegram import (HermesSendSender, InboundMessage, TelegramBridge,
                       TelegramNotifier, ensure_bridge_token)

settings = Settings.load()
logger = logging.getLogger("cortexos.api")
service = CortexService(settings)
vault_browser = VaultBrowser(settings.vault)
voice = LocalVoice(settings)
auth_store = AuthStore(settings.root / "config" / "auth.json")
login_throttle = LoginThrottle()
terminals = TerminalManager(settings, service.vault)
request_streams = RequestStreams()
metrics_bus = MetricsBroadcaster()
background_tasks: dict[str, asyncio.Task] = {}
scheduler_task: asyncio.Task | None = None
backup_task: asyncio.Task | None = None
coordinator = AgentCoordinator(service.registry, service.vault, jev=service.fast_model.jev)
health = HealthCheck(service=service, vault=service.vault, voice=voice)
backups = BackupManager(service.vault)
queue_store: QueueStore | None = None
queue_mgr: QueueManager | None = None
telegram_bridge: TelegramBridge | None = None
telegram_notifier: TelegramNotifier | None = None
telegram_notifier_task: asyncio.Task | None = None
app = FastAPI(title="CortexOS Bridge", version="0.1.0")
hud_dir = settings.root / "apps" / "hud"


def token_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), settings.bridge_token.encode("utf-8"))


PUBLIC_API_PATHS = {"/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout",
                    "/api/telegram/inbound"}


def bearer_matches(request) -> bool:
    supplied = request.headers.get("authorization", "")
    token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
    return bool(token) and token_matches(token)


def session_matches(candidate: str | None) -> bool:
    return bool(candidate) and auth_store.verify_session(candidate)


@app.middleware("http")
async def require_bridge_token(request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in PUBLIC_API_PATHS:
        if not bearer_matches(request) and not session_matches(request.cookies.get(SESSION_COOKIE)):
            return JSONResponse(status_code=401, content={"detail": "Sign in required"},
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


async def authenticate_websocket(websocket: WebSocket) -> bool:
    await websocket.accept()
    if session_matches(websocket.cookies.get(SESSION_COOKIE)):
        return True
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        if message.get("type") == "auth" and token_matches(str(message.get("token", ""))):
            return True
    except (asyncio.TimeoutError, ValueError):
        pass
    await websocket.close(code=4401, reason="Authentication required")
    return False


class AuthLoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class AuthSetupBody(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


def set_session_cookie(response: Response, value: str | None) -> None:
    if not value:
        return
    response.set_cookie(SESSION_COOKIE, value, max_age=SESSION_TTL_SECONDS, path="/",
                        httponly=True, samesite="strict", secure=True)


@app.get("/api/auth/status")
async def auth_status(request: Request):
    authenticated = bearer_matches(request) or session_matches(request.cookies.get(SESSION_COOKIE))
    return {"authenticated": bool(authenticated), **auth_store.status()}


@app.post("/api/auth/setup")
async def auth_setup(body: AuthSetupBody, response: Response):
    if auth_store.has_password():
        raise HTTPException(status_code=409, detail="A dashboard password is already set; sign in instead")
    try:
        auth_store.set_password(body.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_session_cookie(response, auth_store.issue_session())
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(body: AuthLoginBody, request: Request, response: Response):
    key = request.client.host if request.client else "unknown"
    retry_after = login_throttle.retry_after(key)
    if retry_after:
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts; wait before retrying",
                            headers={"Retry-After": str(retry_after)})
    if not auth_store.has_password():
        raise HTTPException(status_code=409, detail="No dashboard password is set yet; complete first-run setup")
    if not auth_store.verify_password(body.password):
        login_throttle.record_failure(key)
        raise HTTPException(status_code=401, detail="Wrong password")
    login_throttle.reset(key)
    set_session_cookie(response, auth_store.issue_session())
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


class RequestBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    backend: str | None = None
    skill_id: str | None = None
    request_id: uuid.UUID | None = None
    background: bool = False


class TaskBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    backend: str | None = None
    skill_id: str | None = None
    priority: int = Field(default=5, ge=1, le=9)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    dedupe_key: str | None = Field(default=None, max_length=200)
    scheduled_for: float | None = Field(default=None, ge=0)


class TelegramInboundBody(BaseModel):
    chat_id: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    user_id: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    message_id: int | None = None
    update_id: int | None = None
    username: Annotated[str | None, StringConstraints(max_length=64)] = None
    chat_type: Annotated[str | None, StringConstraints(max_length=16)] = None
    ts: float | None = None


class FeedbackBody(BaseModel):
    skill_id: str
    useful: bool


class RollbackBody(BaseModel):
    hash: str = Field(min_length=1, max_length=128)
    confirm: bool = False


class VoiceTurnBody(BaseModel):
    audio_base64: Annotated[str, StringConstraints(min_length=1, max_length=28_000_000)]
    content_type: str = "audio/webm"
    backend: str | None = None
    skill_id: str | None = None
    request_id: uuid.UUID | None = None
    background: bool = False
    as_draft: bool = False
    draft_kind: Literal["note", "task", "email", "connector"] = "note"
    draft_target: dict[str, Any] | None = None


class DraftBody(BaseModel):
    kind: Literal["note", "task", "email", "connector"] = "note"
    body: Annotated[str, StringConstraints(min_length=1, max_length=20000)]
    title: Annotated[str, StringConstraints(max_length=200)] | None = None
    target: dict[str, Any] | None = None
    transcript: Annotated[str, StringConstraints(max_length=40000)] | None = None


class DraftUpdateBody(BaseModel):
    title: Annotated[str, StringConstraints(max_length=200)] | None = None
    body: Annotated[str, StringConstraints(min_length=1, max_length=20000)] | None = None
    target: dict[str, Any] | None = None


class SnoozeBody(BaseModel):
    hours: float = Field(default=24, ge=0.1, le=168)


class SuggestionActBody(BaseModel):
    action: str = Field(min_length=1, max_length=40)


class ConnectorOperationBody(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Annotated[str, StringConstraints(max_length=200)] | None = None


class OAuthExchangeBody(BaseModel):
    state: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    code: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class SpeakBody(BaseModel):
    text: Annotated[str, StringConstraints(min_length=1, max_length=5000)]


class TerminalBody(BaseModel):
    backend: str
    cols: int = 100
    rows: int = 28


async def queue_stream_sink(stream_id: str, event: dict, finish: bool) -> None:
    stream = request_streams.get(stream_id)
    if stream is None:
        return
    if finish:
        await stream.finish(event)
    else:
        await stream.publish(event)


async def combined_queue_sink(stream_id: str, event: dict, finish: bool) -> None:
    """Queue events fan out to the request streams (HUD) and, when enabled, to
    the Telegram notifier for origin=telegram tasks. The notifier can never
    break the stream path: it guards itself."""
    await queue_stream_sink(stream_id, event, finish)
    if telegram_notifier is not None:
        await telegram_notifier.on_event(stream_id, event, finish)


async def telegram_reject_fn(request_id: str) -> dict:
    result = service.reject(request_id)
    if queue_mgr is not None:
        await queue_mgr.note_rejection(request_id)
    return result


def setup_telegram() -> None:
    """Build the Telegram bridge/notifier when [telegram] enabled = true.
    The scoped ingress token file is created if missing (0640 root:hermes)."""
    global telegram_bridge, telegram_notifier
    if not settings.telegram_enabled:
        return
    if not ensure_bridge_token(settings.telegram_token_file):
        logger.warning("telegram bridge enabled but the ingress token file could not be created; ingress stays closed")
        return
    telegram_bridge = TelegramBridge(settings)
    sender = HermesSendSender(settings.telegram_send_command,
                              timeout=settings.telegram_send_timeout_seconds)
    telegram_notifier = TelegramNotifier(settings, sender=sender)
    logger.info("telegram bridge enabled: ingress on /api/telegram/inbound, notifications via %s",
                settings.telegram_send_command)


def request_known(request_id: str) -> bool:
    """A request stream may be created on demand only for requests that exist:
    live or recorded vault requests, pending approvals, or queue tasks."""
    if request_id in service.pending or service.vault.request_exists(request_id):
        return True
    if queue_mgr is not None:
        return (queue_mgr.store.get(request_id) is not None
                or queue_mgr.store.find_by_request_id(request_id) is not None)
    return False


async def replay_task_snapshot(stream, request_id: str) -> None:
    """A watcher may connect after the task already moved on (or finished)
    without a stream existing. Send the real current state so the viewer is
    never left waiting on silence, and close the stream if the task is done."""
    if queue_mgr is None:
        return
    task = queue_mgr.store.get(request_id) or queue_mgr.store.find_by_request_id(request_id)
    if task is None:
        return
    await stream.publish({"type": "task", "status": task["status"], "task": task})
    if task["status"] == "completed":
        await stream.finish({"id": task["id"], "status": "completed", "task": task["id"],
                             "result": task.get("result_ref")})
    elif task["status"] in {"failed", "cancelled"}:
        await stream.finish({"id": task["id"], "status": "error", "task": task["id"],
                             "error": task.get("error") or f"task {task['status']}"})


def request_interrupt(request_id: str) -> bool:
    """Stop button: SIGINT the provider process, cancel an in-flight gateway
    call, and/or cooperatively cancel the owning durable queue task."""
    stopped = service.agent.interrupt(request_id)
    llm_stopped = service.gateway.interrupt(request_id)
    queued = queue_mgr is not None and queue_mgr.request_cancel_by_request_id(request_id)
    return bool(stopped or llm_stopped or queued)


async def start_background_request(prompt: str, backend: str | None, skill_id: str | None,
                                   request_id: str | None = None) -> dict:
    request_id = request_id or str(uuid.uuid4())
    if request_id in background_tasks:
        raise HTTPException(status_code=409, detail="Request ID is already active")
    if service.vault.request_exists(request_id):
        raise HTTPException(status_code=409, detail="Request ID has already been used; submit a new request instead.")
    if queue_mgr is not None:
        existing = queue_mgr.store.get(request_id)
        if existing and existing["status"] in ("queued", "running", "awaiting_approval"):
            raise HTTPException(status_code=409, detail="Request ID is already active")
        request_streams.create(request_id)
        outcome = await queue_mgr.enqueue(prompt, backend=backend, skill_id=skill_id,
                                          origin="api-background", task_id=request_id,
                                          dedupe_window=settings.queue_dedupe_window_seconds)
        task = outcome["task"]
        return {"id": request_id, "task_id": task["id"], "status": task["status"],
                "duplicate": not outcome["created"],
                "stream": f"/api/requests/{request_id}/stream", "queued": True}
    # Fallback when the queue is disabled: in-memory background task (lost on restart).
    stream = request_streams.create(request_id)

    async def run_background():
        try:
            result = await service.submit(prompt, backend, skill_id, request_id=request_id,
                                          on_output=lambda text: stream.publish({"type": "output", "data": text}))
        except Exception as exc:
            result = {"id": request_id, "status": "error", "error": str(exc)}
        await stream.finish(result)

    task = asyncio.create_task(run_background())
    background_tasks[request_id] = task
    task.add_done_callback(lambda _: background_tasks.pop(request_id, None))
    return {"id": request_id, "status": "running", "stream": f"/api/requests/{request_id}/stream"}


def start_background_approval(request_id: str) -> dict:
    if request_id in background_tasks:
        raise HTTPException(status_code=409, detail="Request ID is already active")
    stream = request_streams.create(request_id)

    async def run_approval():
        try:
            result = await service.approve(request_id,
                                           on_output=lambda text: stream.publish({"type": "output", "data": text}))
        except Exception as exc:
            result = {"id": request_id, "status": "error", "error": str(exc)}
        if queue_mgr is not None:
            await queue_mgr.note_approval_outcome(request_id, result)
        await stream.finish(result)

    task = asyncio.create_task(run_approval())
    background_tasks[request_id] = task
    task.add_done_callback(lambda _: background_tasks.pop(request_id, None))
    return {"id": request_id, "status": "running", "stream": f"/api/requests/{request_id}/stream"}


async def check_websocket_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    allowed = {f"http://127.0.0.1:{settings.port}", f"http://localhost:{settings.port}",
               f"https://127.0.0.1:{settings.port}", f"https://localhost:{settings.port}",
               f"https://{settings.host}:{settings.port}",
               "app://obsidian.md", "capacitor://localhost"}
    # Any LAN origin is allowed when serving over TLS with the shared bridge token;
    # the token is the access control, not the origin. ponytail: fixed LAN trust, per-origin pins if paranoid.
    if origin in allowed:
        return True
    # Trust any http(s) origin whose host is a LAN address; TLS + token is the gate.
    import ipaddress, urllib.parse
    try:
        host = urllib.parse.urlparse(origin).hostname or ""
        if ipaddress.ip_address(host).is_private and urllib.parse.urlparse(origin).scheme in ("http", "https"):
            return True
    except ValueError:
        pass
    await websocket.close(code=4403, reason="WebSocket origin is not allowed")
    return False


@app.get("/api/status")
def status():
    return {**service.status(), "voice": voice.status()}


@app.get("/api/agents")
def agents():
    llm_agents = [{"id": entry["agent_id"], "name": entry["name"], "kind": "llm",
                   "provider": entry["provider"], "model": entry["model"], "issues": entry.get("issues", [])}
                  for entry in service.gateway.agents_status()]
    return {"default": settings.backend, "agents": [*settings.agents, *llm_agents],
            "coordinator": coordinator.status(), "llm_agents": llm_agents}


class LlmTestBody(BaseModel):
    mode: str = "validate"
    model: str = ""


def _browse_error(exc: BrowseError) -> HTTPException:
    message = str(exc)
    return HTTPException(status_code=404 if "not found" in message else 400, detail=message)


@app.get("/api/vault/tree")
def vault_tree(path: str = ""):
    try:
        return vault_browser.tree(path)
    except BrowseError as exc:
        raise _browse_error(exc) from exc


@app.get("/api/vault/file")
def vault_file(path: str):
    try:
        return vault_browser.file(path)
    except BrowseError as exc:
        raise _browse_error(exc) from exc


@app.get("/api/vault/search")
def vault_search(q: str, limit: int = 50):
    try:
        return vault_browser.search(q, limit)
    except BrowseError as exc:
        raise _browse_error(exc) from exc


@app.get("/api/activity")
def activity(q: str = "", kind: str = "all", status: str = "", limit: int = 20, before: float | None = None):
    return search_activity(settings.queue_db, settings.vault, q=q, kind=kind, status=status,
                           limit=limit, before=before)


@app.get("/api/agents/directory")
def agents_directory_endpoint():
    return {"agents": agents_directory(settings, coordinator, service.gateway, settings.queue_db),
            "generated_at": time.time()}


@app.get("/api/llm/providers")
def llm_providers():
    return {"providers": service.gateway.providers_status(),
            "defaults": {"timeout_seconds": settings.llm_default_timeout_seconds},
            "last_error": service.gateway.last_error}


@app.post("/api/llm/providers/{provider_id}/test")
async def llm_provider_test(provider_id: str, body: LlmTestBody):
    if provider_id not in service.gateway.providers:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if body.mode not in {"validate", "connectivity", "inference"}:
        raise HTTPException(status_code=400, detail="mode must be validate, connectivity, or inference")
    return await service.gateway.test_provider(provider_id, body.mode, body.model)


@app.get("/api/llm/agents")
def llm_agents():
    return {"agents": service.gateway.agents_status(),
            "provider_ids": [provider["id"] for provider in service.gateway.providers_status()]}


class LlmAssignmentBody(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(default="", max_length=120)
    name: str = Field(default="", max_length=80)
    persona: str = Field(default="", max_length=4000)
    fallback: list[dict] = []
    allow_cloud_fallback: bool = False
    requirements: list[str] = ["chat"]


class LlmAgentCreateBody(LlmAssignmentBody):
    agent_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


@app.post("/api/llm/agents")
def llm_agent_create(body: LlmAgentCreateBody):
    """Create a new named bot/agent bound to a model-gateway provider.

    The id becomes the task backend (`/cx assign <id>` and the HUD selector);
    CLI agent ids and existing bots are refused so nothing is silently
    overwritten. Validation, storage, and execution stay in the gateway.
    """
    agent_id = body.agent_id.strip()
    if agent_id in settings.agent_ids:
        raise HTTPException(status_code=409, detail=f"'{agent_id}' is a configured CLI agent id; pick another id")
    if service.gateway.resolve(agent_id) is not None:
        raise HTTPException(status_code=409, detail=f"agent '{agent_id}' already exists; use the assignment form to update it")
    data = body.model_dump()
    data.pop("agent_id", None)
    try:
        entry = service.gateway.save_assignment(agent_id, data)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "created", "agent": entry}


@app.put("/api/llm/agents/{agent_id}")
def llm_agent_put(agent_id: str, body: LlmAssignmentBody):
    if len(body.fallback) > 3:
        raise HTTPException(status_code=400, detail="at most 3 fallback providers")
    try:
        return service.gateway.save_assignment(agent_id, body.model_dump())
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/llm/agents/{agent_id}")
def llm_agent_delete(agent_id: str):
    if not service.gateway.delete_assignment(agent_id):
        raise HTTPException(status_code=404, detail="No assignment for this agent")
    return {"status": "deleted", "agent_id": agent_id}


@app.get("/api/voice/status")
def voice_status():
    return voice.status()


@app.get("/api/sessions")
def list_sessions():
    return [{"id": session.id, "backend": session.backend, "alive": session.exit_code is None,
             "started": session.started, "exit_code": session.exit_code, "sequence": session.sequence}
            for session in terminals.sessions.values()]


@app.post("/api/sessions")
async def create_session(body: TerminalBody):
    try:
        session = await terminals.spawn(body.backend, body.cols, body.rows)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=503, detail=f"Could not start {body.backend}. Install and authenticate its CLI first.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"id": session.id, "backend": session.backend, "status": "starting",
            "websocket": f"/api/sessions/{session.id}/terminal"}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    if not await terminals.terminate(session_id):
        raise HTTPException(status_code=404, detail="Terminal session not found")
    return {"id": session_id, "status": "ended"}


@app.websocket("/api/sessions/{session_id}/terminal")
async def terminal_socket(websocket: WebSocket, session_id: str):
    if not await check_websocket_origin(websocket):
        return
    session = terminals.get(session_id)
    if session is None:
        await websocket.close(code=4404, reason="Terminal session not found")
        return
    if not await authenticate_websocket(websocket):
        return
    await terminal_websocket(websocket, session)


@app.websocket("/api/requests/{request_id}/stream")
async def request_stream_socket(websocket: WebSocket, request_id: str):
    if not await check_websocket_origin(websocket):
        return
    if not await authenticate_websocket(websocket):
        return
    stream = request_streams.get(request_id)
    if stream is None:
        # Tasks submitted directly to the queue have no stream until someone
        # watches them; create it on demand for real requests only.
        if not request_known(request_id):
            await websocket.close(code=4404, reason="Request stream not found")
            return
        stream = request_streams.create(request_id)
        await replay_task_snapshot(stream, request_id)
    await request_streams.serve(websocket, stream, request_interrupt)


@app.on_event("shutdown")
async def shutdown_sessions():
    global queue_mgr, telegram_notifier_task
    if telegram_notifier_task is not None:
        telegram_notifier_task.cancel()
        await asyncio.gather(telegram_notifier_task, return_exceptions=True)
        telegram_notifier_task = None
    if queue_mgr is not None:
        await queue_mgr.stop()
        service.queue = None
        queue_mgr = None
    if scheduler_task:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)
    if backup_task:
        backup_task.cancel()
        await asyncio.gather(backup_task, return_exceptions=True)
    for task in list(background_tasks.values()):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks.values(), return_exceptions=True)
    await terminals.close_all()


@app.on_event("startup")
async def start_scheduler():
    global scheduler_task, backup_task, queue_mgr, queue_store, telegram_notifier_task
    service.vault.bootstrap()
    setup_telegram()
    if settings.queue_enabled:
        queue_store = QueueStore(settings.queue_db)
        queue_mgr = QueueManager(service, queue_store, workers=settings.queue_workers,
                                 poll_interval=settings.queue_poll_interval_seconds,
                                 task_timeout=settings.queue_task_timeout_seconds,
                                 backoff_base=settings.queue_backoff_base_seconds,
                                 backoff_cap=settings.queue_backoff_cap_seconds,
                                 max_attempts=settings.queue_max_attempts,
                                 retention_days=settings.queue_retention_days,
                                 event_sink=combined_queue_sink if telegram_notifier is not None else queue_stream_sink)
        service.queue = queue_mgr
        await queue_mgr.start()
        if telegram_bridge is not None:
            telegram_bridge.bind(queue_mgr, queue_store, service)
            telegram_bridge.approve_fn = start_background_approval
            telegram_bridge.reject_fn = telegram_reject_fn
            telegram_bridge.directory_provider = lambda: agents_directory(
                settings, coordinator, service.gateway, settings.queue_db)
        if telegram_notifier is not None:
            telegram_notifier.store = queue_store
            telegram_notifier_task = asyncio.create_task(telegram_notifier.run(), name="telegram-notifier")
    scheduler_task = asyncio.create_task(run_scheduler(service))
    backup_task = asyncio.create_task(backup_loop(backups))


def _skill_stats_summary(skill):
    stats = service.skill_stats(skill.id, skill=skill)
    stats.pop("recent_runs", None)
    return stats


@app.get("/api/skills")
def skills():
    return [{"id": s.id, "name": s.name, "domain": s.domain, "description": s.description,
             "risk_level": s.risk_level, "approval_required": s.approval_required, "automation_ready": s.automation_ready,
             "version": s.version, **_skill_stats_summary(s)} for s in service.registry.list()]


@app.get("/api/skills/validation")
def skills_validation():
    scan = service.registry.scan()
    return {"valid": len(scan.skills),
            "rejected": sum(1 for item in scan.problems if item.get("rejected")),
            "problems": scan.problems}


@app.get("/api/skills/{skill_id}")
def skill_detail(skill_id: str):
    skill = service.registry.get(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        relative_path = str(skill.path.relative_to(settings.skills.parent))
    except ValueError:
        relative_path = skill.path.name
    files: list[str] = []
    try:
        files = sorted(item.name for item in skill.path.iterdir() if item.is_file())
    except OSError:
        files = []
    return {"id": skill.id, "name": skill.name, "domain": skill.domain, "description": skill.description,
            "triggers": skill.trigger_phrases, "tier": skill.tier, "risk_level": skill.risk_level,
            "approval_required": skill.approval_required, "automation_ready": skill.automation_ready,
            "version": skill.version, "version_source": skill.version_source, "content_hash": skill.content_hash,
            "tests": skill.tests, "problems": skill.problems,
            "path": relative_path, "files": files, "instructions": skill.instructions[:20000],
            **service.skill_stats(skill.id, skill=skill)}


@app.get("/api/skills/{skill_id}/history")
def skill_history(skill_id: str):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.history_view(skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/skills/{skill_id}/history/{target_hash}")
def skill_history_entry(skill_id: str, target_hash: str):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.history_entry(skill_id, target_hash)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/skills/{skill_id}/test")
def run_skill_test(skill_id: str):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.run_skill_tests(skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/skills/{skill_id}/rollback")
def rollback_skill(skill_id: str, body: RollbackBody):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.rollback_skill(skill_id, body.hash, confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/skills/{skill_id}/promote")
def promote_skill(skill_id: str):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.request_promotion(skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/skills/{skill_id}/promote/approve")
def approve_skill_promotion(skill_id: str):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.approve_promotion(skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/metrics")
def metrics():
    return service.vault.metrics()


@app.get("/api/metrics/history")
def metrics_history(limit: int = 60):
    return service.vault.metrics_history(limit)


@app.websocket("/api/metrics/stream")
async def metrics_stream_socket(websocket: WebSocket):
    if not await check_websocket_origin(websocket):
        return
    if not await authenticate_websocket(websocket):
        return
    metrics_bus.subscribe(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        metrics_bus.unsubscribe(websocket)


@app.get("/api/approvals")
def pending_approvals():
    return [{"id": request_id, "skill": item.get("skill_id"), "prompt": item.get("prompt"),
             "risk_level": item.get("risk_level"), "reason": item.get("reason")}
            for request_id, item in service.pending.items()]


@app.post("/api/requests")
async def submit(body: RequestBody):
    if body.background:
        return await start_background_request(body.prompt, body.backend, body.skill_id, str(body.request_id) if body.request_id else None)
    result = await service.submit(body.prompt, body.backend, body.skill_id, str(body.request_id) if body.request_id else None)
    if result.get("status") == "error" and "Request ID has already been used" in result.get("error", ""):
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/api/voice/command")
async def voice_command(body: RequestBody):
    result = await service.submit(body.prompt, body.backend, body.skill_id, str(body.request_id) if body.request_id else None)
    if result.get("status") == "error" and "Request ID has already been used" in result.get("error", ""):
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/api/voice/turn")
async def voice_turn(body: VoiceTurnBody):
    encoded = body.audio_base64
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Audio must be valid base64 data") from exc
    if not audio or len(audio) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio must be between 1 byte and 20 MB")
    try:
        transcript = await voice.transcribe(audio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VoiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not transcript:
        raise HTTPException(status_code=422, detail="Whisper did not detect speech")
    if body.as_draft:
        try:
            draft = service.create_draft(kind=body.draft_kind, body=transcript, transcript=transcript,
                                         target=body.draft_target, source="voice")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": draft["id"], "status": "draft", "transcript": transcript,
                "draft": {**draft, "execution": service.draft_execution(draft)}}
    if body.background:
        result = await start_background_request(transcript, body.backend, body.skill_id, str(body.request_id) if body.request_id else None)
        result["transcript"] = transcript
    else:
        result = await service.submit(transcript, body.backend, body.skill_id, str(body.request_id) if body.request_id else None)
        result["transcript"] = transcript
    return result


@app.post("/api/voice/speak")
async def voice_speak(body: SpeakBody):
    try:
        wav = await voice.synthesize(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VoiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/drafts")
def list_drafts():
    return [{"id": draft["id"], "kind": draft["kind"], "title": draft["title"],
             "target": draft.get("target"), "source": draft.get("source"),
             "status": draft.get("status"), "revision": draft.get("revision"),
             "created_at": draft.get("created_at"), "updated_at": draft.get("updated_at"),
             "has_transcript": bool(draft.get("transcript"))}
            for draft in service.drafts.list()]


@app.post("/api/drafts")
def create_draft(body: DraftBody):
    try:
        draft = service.create_draft(kind=body.kind, body=body.body, title=body.title,
                                     target=body.target, transcript=body.transcript, source="text")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**draft, "execution": service.draft_execution(draft)}


@app.get("/api/drafts/{draft_id}")
def draft_detail(draft_id: str):
    draft = service.drafts.get(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return {**draft, "execution": service.draft_execution(draft)}


@app.put("/api/drafts/{draft_id}")
def update_draft(draft_id: str, body: DraftUpdateBody):
    if not service.drafts.get(draft_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        draft = service.drafts.update(draft_id, title=body.title, body=body.body, target=body.target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**draft, "execution": service.draft_execution(draft)}


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str):
    if not service.drafts.delete(draft_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"id": draft_id, "status": "discarded"}


@app.post("/api/drafts/{draft_id}/save")
async def save_draft(draft_id: str):
    if not service.drafts.get(draft_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        return await service.save_draft(draft_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/drafts/{draft_id}/regenerate")
async def regenerate_draft(draft_id: str):
    if not service.drafts.get(draft_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        draft = await service.regenerate_draft(draft_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**draft, "execution": service.draft_execution(draft)}


@app.get("/api/suggestions")
def suggestions():
    return service.suggestions_report()


@app.post("/api/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(suggestion_id: str):
    try:
        return service.suggestions.dismiss(suggestion_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/suggestions/{suggestion_id}/snooze")
def snooze_suggestion(suggestion_id: str, body: SnoozeBody):
    try:
        return service.suggestions.snooze(suggestion_id, seconds=body.hours * 3600)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/suggestions/{suggestion_id}/act")
async def act_on_suggestion(suggestion_id: str, body: SuggestionActBody):
    try:
        return await service.suggestion_act(suggestion_id, body.action)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/connectors")
def connectors_status():
    return service.connectors.status()


@app.get("/api/connectors/{connector_id}")
def connector_detail(connector_id: str):
    for item in service.connectors.status()["connectors"]:
        if item["id"] == connector_id:
            return item
    raise HTTPException(status_code=404, detail="Connector not found")


@app.post("/api/connectors/{connector_id}/health")
async def connector_health(connector_id: str):
    try:
        return await service.connectors.probe(connector_id)
    except ConnectorValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=scrub_secrets(str(exc))) from exc


@app.post("/api/connectors/{connector_id}/operations/{operation_name}")
async def connector_operation(connector_id: str, operation_name: str, body: ConnectorOperationBody):
    try:
        return await service.request_connector_operation(connector_id, operation_name, body.args,
                                                         body.idempotency_key)
    except ConnectorValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectorApprovalRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConnectorRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc),
                            headers={"Retry-After": str(int(exc.retry_after))}) from exc
    except ConnectorBlocked as exc:
        raise HTTPException(status_code=409, detail=f"{exc.status}: {exc.detail}") from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=scrub_secrets(str(exc))) from exc


@app.post("/api/connectors/{connector_id}/oauth/start")
def connector_oauth_start(connector_id: str):
    try:
        return service.connectors.oauth_start(connector_id)
    except ConnectorValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConnectorBlocked as exc:
        raise HTTPException(status_code=409, detail=f"{exc.status}: {exc.detail}") from exc


@app.post("/api/connectors/{connector_id}/oauth/exchange")
async def connector_oauth_exchange(connector_id: str, body: OAuthExchangeBody):
    try:
        return await service.connectors.oauth_exchange(connector_id, body.state, body.code)
    except ConnectorValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConnectorBlocked as exc:
        raise HTTPException(status_code=409, detail=f"{exc.status}: {exc.detail}") from exc


@app.post("/api/requests/{request_id}/approve")
async def approve(request_id: str, background: bool = False):
    if background:
        return start_background_approval(request_id)
    result = await service.approve(request_id)
    if queue_mgr is not None:
        await queue_mgr.note_approval_outcome(request_id, result)
    return result


@app.post("/api/requests/{request_id}/reject")
async def reject(request_id: str):
    result = service.reject(request_id)
    if queue_mgr is not None:
        await queue_mgr.note_rejection(request_id)
    return result


async def require_telegram_bridge_token(request: Request) -> None:
    """Scoped token gate as a dependency: it must answer before body validation.

    Adjusted as a dependency so an unauthenticated caller with a malformed body
    gets 401, not a 422 schema report.
    """
    if telegram_bridge is None:
        raise HTTPException(status_code=503, detail="Telegram bridge is disabled")
    supplied = request.headers.get(telegram_module.BRIDGE_HEADER) or ""
    if not supplied:
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not telegram_bridge.token_ok(supplied):
        raise HTTPException(status_code=401, detail="Invalid bridge token")


@app.post("/api/telegram/inbound", dependencies=[Depends(require_telegram_bridge_token)])
async def telegram_inbound(body: TelegramInboundBody, request: Request):
    """Scoped ingress for the Hermes gateway Telegram plugin.

    Guarded by its own token file (never the main bridge token), the sender
    allowlist shared with Hermes, and a per-user rate limit. Returns the exact
    reply text for the bot to send back in the originating chat."""
    if not telegram_bridge.user_allowed(body.user_id):
        logger.warning("telegram bridge rejected an unauthorized sender")
        return JSONResponse(status_code=403, content={"detail": "Sender is not authorized"})
    if not telegram_bridge.rate_ok(body.user_id):
        return JSONResponse(status_code=429, content={"detail": "Too many requests"},
                            headers={"Retry-After": "60"})
    if not telegram_module.is_trigger(body.text):
        return {"reply": telegram_bridge.help_text()}
    reply = await telegram_bridge.handle(InboundMessage(
        chat_id=str(body.chat_id), user_id=str(body.user_id), text=body.text,
        message_id=body.message_id, update_id=body.update_id, username=body.username,
        chat_type=body.chat_type, ts=body.ts))
    return {"reply": reply}


@app.post("/api/requests/{request_id}/feedback")
async def feedback(request_id: str, body: FeedbackBody):
    result = service.feedback(request_id, body.skill_id, body.useful)
    service.vault.record_metrics_snapshot()
    await metrics_bus.broadcast({"type": "metrics", "metrics": service.vault.metrics(),
                                 "history": service.vault.metrics_history()})
    return result


@app.post("/api/tasks")
async def create_task(body: TaskBody):
    if queue_mgr is None:
        raise HTTPException(status_code=503, detail="Task queue is disabled (see [queue] in config/cortexos.toml)")
    try:
        outcome = await queue_mgr.enqueue(body.prompt, backend=body.backend, skill_id=body.skill_id,
                                          priority=body.priority, max_attempts=body.max_attempts,
                                          dedupe_key=body.dedupe_key, scheduled_for=body.scheduled_for,
                                          origin="api",
                                          dedupe_window=settings.queue_dedupe_window_seconds)
    except QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": outcome["task"], "duplicate": not outcome["created"]}


@app.get("/api/tasks")
def list_tasks(status: str | None = None, origin: str | None = None, limit: int = 50, offset: int = 0):
    if queue_mgr is None:
        raise HTTPException(status_code=503, detail="Task queue is disabled")
    try:
        tasks = queue_mgr.store.list(status=status, origin=origin, limit=limit, offset=offset)
    except QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tasks": tasks, "summary": queue_mgr.store.stats()}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    if queue_mgr is None:
        raise HTTPException(status_code=503, detail="Task queue is disabled")
    task = queue_mgr.store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task, "events": queue_mgr.store.events(task_id, limit=200)}


@app.get("/api/tasks/{task_id}/events")
def task_events(task_id: str, after_seq: int = 0, limit: int = 200):
    if queue_mgr is None:
        raise HTTPException(status_code=503, detail="Task queue is disabled")
    if queue_mgr.store.get(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"events": queue_mgr.store.events(task_id, after_seq=after_seq, limit=limit)}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    if queue_mgr is None:
        raise HTTPException(status_code=503, detail="Task queue is disabled")
    try:
        return await queue_mgr.cancel(task_id)
    except QueueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    if queue_mgr is None:
        raise HTTPException(status_code=503, detail="Task queue is disabled")
    try:
        return await queue_mgr.retry(task_id)
    except QueueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/queue/summary")
def queue_summary():
    if queue_mgr is None:
        return {"enabled": False}
    return {"enabled": True, "summary": queue_mgr.store.stats(),
            "workers": queue_mgr.store.workers(), "recovery": queue_mgr.recovery_report}


@app.get("/api/health")
def health_check():
    return health.snapshot()


@app.get("/api/agents/route")
async def agent_route(prompt: str):
    agent = await coordinator.route(prompt)
    if agent is None:
        return {"agent": None}
    return {"agent": {"id": agent.id, "name": agent.name, "role": agent.role, "lane": agent.lane}}


@app.post("/api/backups")
def create_backup():
    path = backups.create()
    return {"status": "created", "path": str(path)}


@app.get("/api/backups")
def list_backups():
    return backups.list()


if hud_dir.is_dir():
    app.mount("/", StaticFiles(directory=hud_dir, html=True), name="hud")
