#!/usr/bin/env python3
"""Live check: /cx assign routes a task to an explicit agent and completes."""
import json
import re
import ssl
import time
import urllib.error
import urllib.request

CTX = ssl._create_unverified_context()
BRIDGE = open("/etc/cortexos/telegram-bridge.token").read().strip()
MAIN = open("/opt/cortexos/app/config/cortexos.token").read().strip()
NOTIFY_FILE = "/opt/cortexos/app/state/telegram-notify.json"


def http(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://127.0.0.1:8765" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, context=CTX, timeout=40) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


ALLOWED = None
for line in open("/home/hermes/.hermes/.env", encoding="utf-8", errors="replace"):
    if line.startswith("TELEGRAM_ALLOWED_USERS="):
        ALLOWED = re.split(r"[,\s;]+", line.split("=", 1)[1].strip())[0]
        break
if not ALLOWED:
    raise SystemExit("no allowlist")

MID = int(time.time() * 10) % 10 ** 9
status, body = http("POST", "/api/telegram/inbound", {
    "chat_id": ALLOWED, "user_id": ALLOWED, "text": "/cx assign hermes show metrics",
    "message_id": MID, "update_id": MID, "chat_type": "private",
}, token=BRIDGE)
reply = (body or {}).get("reply", "")
print(f"inbound http={status} reply={reply[:120]!r}")
match = re.search(r"Task ([0-9a-f]{8})", reply)
ok_reply = status == 200 and "queued for hermes" in reply and bool(match)
print("ACK:", "ok" if ok_reply else "FAIL")

short = match.group(1) if match else ""
task = None
deadline = time.time() + 150
while time.time() < deadline:
    _, listing = http("GET", "/api/tasks?origin=telegram&limit=50", token=MAIN)
    task = next((t for t in (listing.get("tasks") or []) if t["id"].startswith(short)), None)
    if task and task["status"] in ("completed", "failed", "cancelled"):
        break
    time.sleep(3)

print("task:", {k: (task or {}).get(k) for k in ("id", "status", "backend")})
print("COMPLETED:", "ok" if task and task["status"] == "completed" else "FAIL")
print("AGENT RECORDED:", "ok" if task and task.get("backend") == "hermes" else "FAIL")

stage_ok = False
deadline = time.time() + 45
while time.time() < deadline:
    try:
        state = json.loads(open(NOTIFY_FILE).read())
    except (OSError, ValueError):
        state = {}
    if task and "completed" in (state.get(task["id"]) or []):
        stage_ok = True
        break
    time.sleep(3)
print("NOTIFIED:", "ok" if stage_ok else "FAIL")
raise SystemExit(0 if (ok_reply and task and task["status"] == "completed"
                       and task.get("backend") == "hermes" and stage_ok) else 1)
