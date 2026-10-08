from __future__ import annotations

import base64
import binascii
import asyncio
import uuid
import hmac

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated

from .config import Settings
from .service import CortexService
from .sessions import TerminalManager, terminal_websocket
from .streaming import RequestStreams
from .scheduler import run_scheduler
from .voice import LocalVoice, VoiceUnavailable

settings = Settings.load()
service = CortexService(settings)
voice = LocalVoice(settings)
terminals = TerminalManager(settings, service.vault)
request_streams = RequestStreams()
background_tasks: dict[str, asyncio.Task] = {}
scheduler_task: asyncio.Task | None = None
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
            return Response(status_code=401, content="Bridge token required", headers={"WWW-Authenticate": "Bearer"})
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


def start_background_request(prompt: str, backend: str | None, skill_id: str | None, request_id: str | None = None) -> dict:
    request_id = request_id or str(uuid.uuid4())
    if request_id in background_tasks:
        raise HTTPException(status_code=409, detail="Request ID is already active")
    if service.vault.request_exists(request_id):
        raise HTTPException(status_code=409, detail="Request ID has already been used; submit a new request instead.")
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
               "app://obsidian.md", "capacitor://localhost"}
    if origin not in allowed:
        await websocket.close(code=4403, reason="WebSocket origin is not allowed")
        return False
    return True


@app.get("/api/status")
def status():
    return {**service.status(), "voice": voice.status()}


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
    stream = request_streams.get(request_id)
    if stream is None:
        await websocket.close(code=4404, reason="Request stream not found")
        return
    if not await authenticate_websocket(websocket):
        return
    await request_streams.serve(websocket, stream, service.agent.interrupt)


@app.on_event("shutdown")
async def shutdown_sessions():
    if scheduler_task:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)
    for task in list(background_tasks.values()):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks.values(), return_exceptions=True)
    await terminals.close_all()


@app.on_event("startup")
async def start_scheduler():
    global scheduler_task
    service.vault.bootstrap()
    scheduler_task = asyncio.create_task(run_scheduler(service))


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


@app.get("/api/approvals")
def pending_approvals():
    return [{"id": request_id, "skill": item.get("skill_id"), "prompt": item.get("prompt"),
             "risk_level": item.get("risk_level"), "reason": item.get("reason")}
            for request_id, item in service.pending.items()]


@app.post("/api/requests")
async def submit(body: RequestBody):
    if body.background:
        return start_background_request(body.prompt, body.backend, body.skill_id, str(body.request_id) if body.request_id else None)
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
        result = start_background_request(transcript, body.backend, body.skill_id, str(body.request_id) if body.request_id else None)
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
    return await service.approve(request_id)


@app.post("/api/requests/{request_id}/reject")
def reject(request_id: str):
    return service.reject(request_id)


@app.post("/api/requests/{request_id}/feedback")
def feedback(request_id: str, body: FeedbackBody):
    return service.feedback(request_id, body.skill_id, body.useful)


if hud_dir.is_dir():
    app.mount("/", StaticFiles(directory=hud_dir, html=True), name="hud")
