#!/usr/bin/env python3
"""Live E2E continuation (idempotent): resume durability + approval gate."""
import json
import re
import ssl
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
    except urllib.error.URLError as exc:
        return 0, {"detail": f"unreachable: {exc}"}


def inbound(text, message_id):
    return http("POST", "/api/telegram/inbound", {
        "chat_id": ALLOWED, "user_id": ALLOWED, "text": text,
        "message_id": message_id, "update_id": message_id, "chat_type": "private",
    }, token=BRIDGE)


def reply_of(body):
    return (body or {}).get("reply", "") if isinstance(body, dict) else str(body)


def telegram_tasks():
    _, body = http("GET", "/api/tasks?origin=telegram&limit=50", token=MAIN)
    return (body or {}).get("tasks", [])


def find_task(short_id):
    for task in telegram_tasks():
        if task["id"].startswith(short_id):
            return task
    return None


def wait_for(task_id_short, wanted, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = find_task(task_id_short)
        if task and task["status"] in wanted:
            return task
        time.sleep(3)
    return find_task(task_id_short)


def notified(task_id, stage, timeout=60):
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


def read_allowed():
    for line in open("/home/hermes/.hermes/.env", encoding="utf-8", errors="replace"):
        if line.startswith("TELEGRAM_ALLOWED_USERS="):
            return re.split(r"[,\s;]+", line.split("=", 1)[1].strip())[0]
    raise SystemExit("no TELEGRAM_ALLOWED_USERS")


ALLOWED = read_allowed()
STAMP = int(time.time())

# ---- 1. resume durability: the pre-restart queued task must finish + notify ----
status, body = inbound("/cx resume", STAMP * 10 + 1)
check("R1 resume accepted", status == 200 and "resumed" in reply_of(body).lower(), reply_of(body)[:140])

metrics_tasks = [t for t in telegram_tasks() if "show metrics" in (t.get("prompt") or "")]
check("R2 the pre-restart queued task exists", bool(metrics_tasks))
if metrics_tasks:
    task = metrics_tasks[0]
    if task["status"] == "queued":
        task = wait_for(task["id"][:8], {"completed", "failed", "cancelled"}, timeout=150)
    check("R3 resumed task completed", task["status"] == "completed", task["status"])
    check("R4 notifier sent for the resumed task", notified(task["id"], "completed"))

# ---- 2. approval gate (reuse the interrupted run's task if still pending) ----
email_tasks = [t for t in telegram_tasks()
               if (t.get("prompt") or "").startswith("send an email to test@example.com")]
task = next((t for t in email_tasks if t["status"] == "awaiting_approval"), None)
short = task["id"][:8] if task else ""
if task is None:
    status, body = inbound("/task send an email to test@example.com saying the deployment is green",
                           STAMP * 10 + 2)
    match = re.search(r"Task ([0-9a-f]{8})", reply_of(body))
    short = match.group(1) if match else ""
    check("A1 external-action task accepted", bool(short), reply_of(body)[:160])
    task = wait_for(short, {"awaiting_approval", "failed", "completed", "cancelled"}, timeout=120) if short else None
else:
    check("A1 external-action task accepted (existing)", True)

status_now = (task or {}).get("status")
error_now = str((task or {}).get("error") or "")[:120]
check("A2 approval gate holds the task", status_now == "awaiting_approval",
      f"status={status_now} err={error_now}")
check("A3 notifier sent the approval prompt", notified(task["id"], "approval") if task else False)

if status_now == "awaiting_approval":
    status, body = inbound(f"/cx reject {short}", STAMP * 10 + 3)
    check("A4 /cx reject accepted", status == 200 and "rejected" in reply_of(body).lower(),
          reply_of(body)[:160])
    task = wait_for(short, {"cancelled"}, timeout=60)
    check("A5 task cancelled, never executed",
          bool(task) and task["status"] == "cancelled" and "reject" in (task.get("error") or "").lower(),
          str((task or {}).get("status")))
    check("A6 notifier sent the rejection notice", notified(task["id"], "rejected") if task else False)

# ---- 3. final status ----
status, body = inbound("/cx status", STAMP * 10 + 4)
check("S1 final status shows dispatch running", status == 200 and "Dispatch: running" in reply_of(body),
      reply_of(body)[:200])

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} continuation checks passed")
if failed:
    print("failures: " + ", ".join(failed))
raise SystemExit(1 if failed else 0)
