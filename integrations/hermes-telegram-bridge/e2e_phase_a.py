#!/usr/bin/env python3
"""Live E2E for the CortexOS Telegram bridge (backend side).

Exercises the real HTTP ingress with the scoped bridge token against the live
service: auth, allowlist, commands, queue dedupe, completion + notifier send,
approval rejection, pause durability across a service restart, and cancel.
Never prints tokens or chat ids.

Run on the CortexOS container as root:
    python3 /tmp/tg_live_e2e.py
"""
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request

BASE = "https://127.0.0.1:8765"
CTX = ssl._create_unverified_context()
BRIDGE = open("/etc/cortexos/telegram-bridge.token").read().strip()
MAIN = open("/opt/cortexos/app/config/cortexos.token").read().strip()
NOTIFY_FILE = "/opt/cortexos/app/state/telegram-notify.json"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail and not ok else ""))


def http(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, context=CTX, timeout=40) as response:
            raw = response.read().decode()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def inbound(text, message_id, user_id=None, token=None):
    return http("POST", "/api/telegram/inbound", {
        "chat_id": user_id or ALLOWED,
        "user_id": user_id or ALLOWED,
        "text": text,
        "message_id": message_id,
        "update_id": message_id,
        "chat_type": "private",
    }, token=token if token is not None else BRIDGE)


def read_allowed():
    for line in open("/home/hermes/.hermes/.env", encoding="utf-8", errors="replace"):
        if line.startswith("TELEGRAM_ALLOWED_USERS="):
            return re.split(r"[,\s;]+", line.split("=", 1)[1].strip())[0]
    raise SystemExit("no TELEGRAM_ALLOWED_USERS found")


ALLOWED = read_allowed()
MASKED = ALLOWED[:2] + "…"
print(f"allowed sender: {MASKED}  (ids masked)")

STAMP = int(time.time())
M_A = STAMP * 10 + 1
M_B = STAMP * 10 + 2
M_C = STAMP * 10 + 3
M_D = STAMP * 10 + 4


def find_task(short_id):
    _, body = http("GET", "/api/tasks?origin=telegram&limit=50", token=MAIN)
    for task in body.get("tasks", []):
        if task["id"].startswith(short_id):
            return task
    return None


def wait_status(short_id, wanted, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = find_task(short_id)
        if task and task["status"] in wanted:
            return task
        time.sleep(3)
    return find_task(short_id)


def notified(task_id, stage, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = json.loads(open(NOTIFY_FILE, encoding="utf-8").read())
        except (OSError, ValueError):
            data = {}
        if stage in (data.get(task_id) or []):
            return True
        time.sleep(3)
    return False


def reply_of(body):
    return (body or {}).get("reply", "") if isinstance(body, dict) else str(body)


# ---------------------------------------------------------------- auth layer
status, _ = inbound("/cx status", M_A, token="wrong-token")
check("1 wrong bridge token rejected (401)", status == 401, f"got {status}")

status, _ = inbound("/cx status", M_A, user_id="999999999")
check("2 unauthorized sender rejected (403)", status == 403, f"got {status}")

status, body = inbound("/cx status", M_A)
check("3 /cx status accepted (200)", status == 200, f"got {status}")
check("3b status text sane", "CortexOS status" in reply_of(body) and "Queue:" in reply_of(body),
      reply_of(body)[:120])

# ------------------------------------------------------------ task + dedupe
status, body = inbound("/task list skills", M_A)
reply = reply_of(body)
match = re.search(r"Task ([0-9a-f]{8})", reply)
check("4 /task queued with ack", status == 200 and bool(match), reply[:160])
short_a = match.group(1) if match else ""
task_a = find_task(short_a) if short_a else None
check("4b task persisted origin=telegram", bool(task_a) and task_a["origin"] == "telegram",
      str(task_a)[:200] if task_a else "not found")

status, body = inbound("/task list skills", M_A)
check("5 duplicate message id dedupes", "Already queued" in reply_of(body) and short_a in reply_of(body),
      reply_of(body)[:160])
_, listing = http("GET", "/api/tasks?origin=telegram&limit=50", token=MAIN)
same_key = [t for t in listing.get("tasks", [])
            if isinstance(t.get("meta"), dict)
            and (t["meta"].get("telegram") or {}).get("message_id") == M_A]
check("5b exactly one task for that message", len(same_key) == 1, f"count={len(same_key)}")

# --------------------------------------------------- completion + notifier
task_a = wait_status(short_a, {"completed", "failed", "cancelled"}, timeout=150)
check("6 tier-1 task completed", task_a and task_a["status"] == "completed",
      (task_a or {}).get("status", "missing"))
check("6b notifier recorded a completed send", notified(task_a["id"], "completed") if task_a else False)

# ----------------------------------------------------- approval + rejection
status, body = inbound("/task draft a short welcome message for the new team channel", M_B)
match = re.search(r"Task ([0-9a-f]{8})", reply_of(body))
short_b = match.group(1) if match else ""
check("7 approval-gated task accepted", bool(short_b), reply_of(body)[:160])
task_b = wait_status(short_b, {"awaiting_approval"}, timeout=90) if short_b else None
check("7b task waits for approval", task_b and task_b["status"] == "awaiting_approval",
      (task_b or {}).get("status", "missing"))
check("7c notifier recorded the approval prompt", notified(task_b["id"], "approval") if task_b else False)

status, body = inbound(f"/cx reject {short_b}", M_B + 90)
check("8 /cx reject accepted", status == 200 and "rejected" in reply_of(body).lower(), reply_of(body)[:160])
task_b = wait_status(short_b, {"cancelled"}, timeout=60) if short_b else None
check("8b task cancelled, never executed",
      task_b and task_b["status"] == "cancelled" and "reject" in (task_b.get("error") or "").lower(),
      (task_b or {}).get("status", "missing"))
check("8c notifier recorded rejection", notified(task_b["id"], "rejected") if task_b else False)

# ------------------------------------------------------- status + taskstatus
status, body = inbound(f"/taskstatus {short_a}", M_A + 90)
check("9 /taskstatus shows completion", status == 200 and "completed" in reply_of(body)
      and "Result" in reply_of(body), reply_of(body)[:160])
status, body = inbound("/agents", M_A + 91)
check("9b /agents lists hermes", status == 200 and "hermes" in reply_of(body), reply_of(body)[:160])

# -------------------------------------------- pause, cancel, restart, resume
status, body = inbound("/cx pause", M_A + 92)
check("10 pause accepted", status == 200 and "paused" in reply_of(body).lower(), reply_of(body)[:120])

status, body = inbound("/task show metrics", M_C)
match = re.search(r"Task ([0-9a-f]{8})", reply_of(body))
short_c = match.group(1) if match else ""
status, body = inbound("/task help", M_D)
match = re.search(r"Task ([0-9a-f]{8})", reply_of(body))
short_d = match.group(1) if match else ""
check("10b tasks queued while paused", bool(short_c) and bool(short_d), f"c={short_c} d={short_d}")
time.sleep(6)
task_c = find_task(short_c) if short_c else None
check("10c queued task stays queued while paused", task_c and task_c["status"] == "queued",
      (task_c or {}).get("status", "missing"))

status, body = inbound(f"/cancel {short_d}", M_A + 93)
check("11 cancel queued task refused-by-pause name", status == 200 and "cancelled before it ran" in reply_of(body),
      reply_of(body)[:160])
task_d = find_task(short_d) if short_d else None
check("11b cancelled task really cancelled", task_d and task_d["status"] == "cancelled",
      (task_d or {}).get("status", "missing"))

print("restarting cortexos service for durability check…")
subprocess.run(["env", "XDG_RUNTIME_DIR=/run/user/0", "systemctl", "--user", "restart", "cortexos"],
               timeout=60, check=False)
deadline = time.time() + 60
while time.time() < deadline:
    status, _ = http("GET", "/api/status", token=MAIN)
    if status == 200:
        break
    time.sleep(2)
check("12 service back after restart", status == 200, f"last={status}")

status, body = inbound("/cx status", M_A + 94)
reply = reply_of(body)
check("12b pause survived restart", "paused" in reply.lower(), reply[:160])
task_c = find_task(short_c) if short_c else None
check("12c queued task survived restart", task_c and task_c["status"] == "queued",
      (task_c or {}).get("status", "missing"))

status, body = inbound("/cx resume", M_A + 95)
check("13 resume accepted", status == 200 and "resumed" in reply_of(body).lower(), reply_of(body)[:120])
task_c = wait_status(short_c, {"completed", "failed", "cancelled"}, timeout=150) if short_c else None
check("13b resumed task completed", task_c and task_c["status"] == "completed",
      (task_c or {}).get("status", "missing"))
check("13c notifier sent once for the resumed task", notified(task_c["id"], "completed") if task_c else False)

# Duplicate-notification guard across restart: exactly one completed entry.
try:
    state = json.loads(open(NOTIFY_FILE, encoding="utf-8").read())
except (OSError, ValueError):
    state = {}
if task_c:
    stages = state.get(task_c["id"]) or []
    check("14 no duplicate notifications", stages.count("completed") == 1, str(stages))
if task_a:
    check("14b task A single completed notification", (state.get(task_a["id"]) or []).count("completed") == 1,
          str(state.get(task_a["id"])))

# --------------------------------------------------------------------- done
failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} live checks passed")
if failed:
    print("failures: " + ", ".join(failed))
raise SystemExit(1 if failed else 0)
