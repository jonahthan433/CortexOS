from __future__ import annotations

import base64
import binascii
import asyncio
import time
import uuid
import hmac

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated

from .config import Settings
from .service import CortexService
from .providers import ProviderError
from .browse import VaultBrowser, BrowseError, agents_directory, search_activity
from .sessions import TerminalManager, terminal_websocket
from .streaming import RequestStreams, MetricsBroadcaster
from .scheduler import run_scheduler
from .queue import QueueManager, QueueStore, QueueError
from .agents import AgentCoordinator
from .ops import HealthCheck, BackupManager, backup_loop
from .voice import LocalVoice, VoiceUnavailable

settings = Settings.load()
service = CortexService(settings)
vault_browser = VaultBrowser(settings.vault)
voice = LocalVoice(settings)
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
app = FastAPI(title="CortexOS Bridge", version="0.1.0")
hud_dir = settings.root / "apps" / "hud"


def token_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), settings.bridge_token.encode("utf-8"))


@app.middleware("http")
async def require_bridge_token(request, call_next):
    if request.url.path.startswith("/api/"):
        supplied = request.headers.get("authorization", "")
        token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
        if not token or not token_matches(token):
            return JSONResponse(status_code=401, content={"detail": "Bridge token required"},
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


async def authenticate_websocket(websocket: WebSocket) -> bool:
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        if message.get("type") == "auth" and token_matches(str(message.get("token", ""))):
            return True
    except (asyncio.TimeoutError, ValueError):
        pass
    await websocket.close(code=4401, reason="Bridge token required")
    return False


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


class FeedbackBody(BaseModel):
    skill_id: str
    useful: bool


class VoiceTurnBody(BaseModel):
    audio_base64: Annotated[str, StringConstraints(min_length=1, max_length=28_000_000)]
    content_type: str = "audio/webm"
    backend: str | None = None
    skill_id: str | None = None
    request_id: uuid.UUID | None = None
    background: bool = False


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
    fallback: list[dict] = []
    allow_cloud_fallback: bool = False
    requirements: list[str] = ["chat"]


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
    global queue_mgr
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
    global scheduler_task, backup_task, queue_mgr, queue_store
    service.vault.bootstrap()
    if settings.queue_enabled:
        queue_store = QueueStore(settings.queue_db)
        queue_mgr = QueueManager(service, queue_store, workers=settings.queue_workers,
                                 poll_interval=settings.queue_poll_interval_seconds,
                                 task_timeout=settings.queue_task_timeout_seconds,
                                 backoff_base=settings.queue_backoff_base_seconds,
                                 backoff_cap=settings.queue_backoff_cap_seconds,
                                 max_attempts=settings.queue_max_attempts,
                                 retention_days=settings.queue_retention_days,
                                 event_sink=queue_stream_sink)
        service.queue = queue_mgr
        await queue_mgr.start()
    scheduler_task = asyncio.create_task(run_scheduler(service))
    backup_task = asyncio.create_task(backup_loop(backups))


@app.get("/api/skills")
def skills():
    return [{"id": s.id, "name": s.name, "domain": s.domain, "description": s.description,
             "risk_level": s.risk_level, "approval_required": s.approval_required, "automation_ready": s.automation_ready,
             **service.vault.skill_stats(s.id)} for s in service.registry.list()]


@app.post("/api/skills/{skill_id}/promote")
def promote_skill(skill_id: str):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.vault.promote_skill(skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/skills/{skill_id}/promote/approve")
def approve_skill_promotion(skill_id: str):
    if not service.registry.get(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    try:
        return service.vault.approve_promotion(skill_id)
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
