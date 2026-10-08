from __future__ import annotations

import asyncio
import logging
import tomllib
import time
import uuid
from datetime import datetime, timezone


async def scheduler_tick(service, dry_run: bool = False, now_epoch: float | None = None) -> list[dict]:
    """Evaluate due jobs; persist their claim before dispatch to avoid restart replays."""
    config_path = service.settings.vault / "automations.toml"
    if not config_path.exists():
        return []
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    current = time.time() if now_epoch is None else now_epoch
    state = service.vault.load_state()
    last = state.setdefault("scheduler_last_run", {})
    due = []
    for index, job in enumerate(config.get("jobs", [])):
        skill_id = str(job.get("skill_id", ""))
        interval = max(60, int(job.get("interval_minutes", 0)) * 60)
        if not job.get("enabled", False) or not skill_id or not job.get("prompt"):
            continue
        skill = service.registry.get(skill_id)
        if not skill or service.vault.promotion(skill_id).get("status") != "approved":
            continue
        key = str(job.get("id") or f"{skill_id}-{index}")
        if any(item.get("skill_id") == skill_id for item in service.pending.values()):
            continue
        if current - float(last.get(key, 0)) < interval:
            continue
        due.append({"id": key, "skill_id": skill_id, "prompt": str(job["prompt"]), "interval_seconds": interval})
    if dry_run:
        return due
    for job in due:
        # Claim before dispatch: a crash can skip one scheduled run, but cannot silently replay it.
        last[job["id"]] = current
        service.vault.write_state(state)
        request_id = f"scheduled-{job['id']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        await service.submit(job["prompt"], skill_id=job["skill_id"], request_id=request_id)
    return due


async def run_scheduler(service, interval_seconds: int = 30) -> None:
    while True:
        try:
            await scheduler_tick(service)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("cortexos.scheduler").exception("Scheduler tick failed")
        await asyncio.sleep(interval_seconds)
