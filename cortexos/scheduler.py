from __future__ import annotations

import asyncio
import logging
import tomllib
from datetime import datetime, timezone


async def run_scheduler(service, interval_seconds: int = 30) -> None:
    """Run explicitly configured, approved jobs; an absent/empty file is inert."""
    last_run: dict[str, float] = {}
    while True:
        try:
            config_path = service.settings.vault / "automations.toml"
            if config_path.exists():
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
                now = asyncio.get_running_loop().time()
                for job in config.get("jobs", []):
                    skill_id = str(job.get("skill_id", ""))
                    interval = max(60, int(job.get("interval_minutes", 0)) * 60)
                    if not job.get("enabled", False) or not skill_id or not job.get("prompt"):
                        continue
                    skill = service.registry.get(skill_id)
                    if not skill or service.vault.promotion(skill_id).get("status") != "approved":
                        continue
                    key = str(job.get("id") or skill_id)
                    if any(item.get("skill_id") == skill_id for item in service.pending.values()):
                        continue
                    if now - last_run.get(key, 0) < interval:
                        continue
                    last_run[key] = now
                    request_id = f"scheduled-{key}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                    # submit() continues to enforce per-run approval_required and action intent.
                    await service.submit(str(job["prompt"]), skill_id=skill_id, request_id=request_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep a malformed job from stopping future ticks and leave an operator clue.
            logging.getLogger("cortexos.scheduler").exception("Scheduler tick failed")
        await asyncio.sleep(interval_seconds)
