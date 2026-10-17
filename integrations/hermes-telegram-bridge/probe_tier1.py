#!/usr/bin/env python3
"""Live probe: which deterministic prompts complete without providers?"""
import json
import ssl
import time
import urllib.request

CTX = ssl._create_unverified_context()
MAIN = open("/opt/cortexos/app/config/cortexos.token").read().strip()


def req(method, path, body=None):
    request = urllib.request.Request(
        "https://127.0.0.1:8765" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + MAIN, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, context=CTX, timeout=30) as response:
        return json.loads(response.read().decode())


def task_of(out):
    return out.get("task") or out


stamp = int(time.time())
ids = []
for index, prompt in enumerate(["list skills", "show metrics"]):
    out = req("POST", "/api/tasks", {"prompt": prompt, "dedupe_key": f"probe-{stamp}-{index}"})
    task = task_of(out)
    ids.append(task["id"])
    print(f"submitted {prompt!r} -> {task['id'][:8]} status={task['status']}")

deadline = time.time() + 90
final = []
while time.time() < deadline:
    states = []
    for task_id in ids:
        detail = task_of(req("GET", f"/api/tasks/{task_id}"))
        states.append({"id": task_id[:8], "status": detail.get("status"),
                       "tier": (detail.get("meta") or {}).get("tier"),
                       "error": (detail.get("error") or "")[:70],
                       "result": (detail.get("result_ref") or "")[:70]})
    final = states
    print(json.dumps(states, ensure_ascii=False))
    if all(s["status"] in ("completed", "failed", "cancelled") for s in states):
        break
    time.sleep(6)
print("TERMINAL:", all(s["status"] in ("completed", "failed", "cancelled") for s in final))
