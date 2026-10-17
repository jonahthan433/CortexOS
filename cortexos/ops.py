"""Health checks, monitoring, and backup/restore for CortexOS."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cortexos.ops")


class HealthCheck:
    """Aggregates component health for /api/health."""

    def __init__(self, service=None, vault=None, voice=None):
        self.service = service
        self.vault = vault
        self.voice = voice
        self.started = time.time()
        self.last_error: str | None = None

    def snapshot(self) -> dict:
        checks = {
            "bridge": {"status": "ok", "uptime_seconds": int(time.time() - self.started)},
            "vault": self._check_vault(),
            "skills": self._check_skills(),
            "voice": self._check_voice(),
            "router": self._check_router(),
            "queue": self._check_queue(),
            "llm": self._check_llm(),
        }
        overall = "ok" if all(c.get("status") == "ok" for c in checks.values()) else "degraded"
        return {"status": overall, "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks}

    def _check_vault(self) -> dict:
        if not self.vault:
            return {"status": "unknown"}
        try:
            state = self.vault.load_state()
            pending = len(state.get("pending_approvals", {}))
            return {"status": "ok", "pending_approvals": pending}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _check_skills(self) -> dict:
        if not self.service:
            return {"status": "unknown"}
        try:
            count = len(self.service.registry.list())
            return {"status": "ok", "count": count}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _check_voice(self) -> dict:
        if not self.voice:
            return {"status": "unknown"}
        try:
            status = self.voice.status()
            ready = status.get("stt") == "ready" and status.get("tts") == "ready"
            return {"status": "ok" if ready else "degraded", "stt": status.get("stt"), "tts": status.get("tts")}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _check_router(self) -> dict:
        """Report configured routing backends and the most recent provider failure."""
        if not self.service:
            return {"status": "unknown"}
        try:
            fast = self.service.fast_model
            last_error = fast.last_error or fast.jev.last_error
            detail = {"fast_model_enabled": fast.settings.fast_model_enabled,
                      "jev_enabled": fast.settings.jev_enabled}
            if last_error:
                detail.update({"status": "degraded", "last_error": last_error})
            else:
                detail["status"] = "ok"
            return detail
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _check_llm(self) -> dict:
        """Universal provider layer: registry, assignments, recent failures."""
        if not self.service or not getattr(self.service, "gateway", None):
            return {"status": "unknown"}
        try:
            gateway = self.service.gateway
            providers = gateway.providers
            enabled = [spec for spec in providers.values() if spec.enabled]
            degraded = [spec.id for spec in enabled
                        if (gateway.health.get(spec.id) or {}).get("status") == "error"]
            detail: dict = {"status": "ok", "providers": len(providers), "enabled": len(enabled),
                            "assigned_agents": len(gateway.agent_ids)}
            if degraded:
                detail.update({"status": "degraded", "degraded_providers": degraded})
            if gateway.last_error:
                detail["last_error"] = gateway.last_error
            return detail
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _check_queue(self) -> dict:
        """Durable queue depth plus worker heartbeats."""
        queue = getattr(self.service, "queue", None) if self.service else None
        if queue is None:
            return {"status": "disabled"}
        try:
            stats = queue.store.stats()
            workers = queue.store.workers()
            alive = [worker for worker in workers if worker.get("alive")]
            return {"status": "ok" if alive else "degraded",
                    "workers_alive": len(alive), "workers_total": len(workers), **stats}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


class BackupManager:
    """Vault backups: timestamped copies of state.json + receipts, capped at N."""

    MAX_BACKUPS = 10

    def __init__(self, vault: Vault, backup_dir: Path | None = None):
        self.vault = vault
        self.backup_dir = backup_dir or (vault.root / ".backups")

    def create(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.backup_dir / f"vault-{stamp}"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("state.json", "automations.toml"):
            src = self.vault.root / name
            if src.exists():
                shutil.copy2(src, target / name)
        for folder in ("receipts", "requests", "metrics"):
            src = self.vault.root / folder
            if src.exists():
                shutil.copytree(src, target / folder, dirs_exist_ok=True)
        self._prune()
        logger.info("backup created: %s", target)
        return target

    def list(self) -> list[dict]:
        if not self.backup_dir.exists():
            return []
        return [{"name": p.name, "created": p.stat().st_mtime}
                for p in sorted(self.backup_dir.iterdir(), reverse=True) if p.is_dir()]

    def restore(self, name: str) -> bool:
        src = self.backup_dir / name
        if not src.exists():
            return False
        for item in src.iterdir():
            dest = self.vault.root / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        logger.info("backup restored: %s", name)
        return True

    def _prune(self) -> None:
        backups = sorted((p for p in self.backup_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
        while len(backups) > self.MAX_BACKUPS:
            shutil.rmtree(backups.pop(0), ignore_errors=True)


async def backup_loop(manager: BackupManager, interval_seconds: int = 3600) -> None:
    """Hourly backup. ponytail: fixed interval, cron-grade if needed later."""
    while True:
        try:
            await asyncio.to_thread(manager.create)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("backup failed")
        await asyncio.sleep(interval_seconds)
