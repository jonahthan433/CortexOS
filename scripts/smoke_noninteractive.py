#!/usr/bin/env python3
"""Noninteractive CortexOS checks using a temporary vault and a stub agent."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from cortexos.config import Settings
from cortexos.scheduler import scheduler_tick
from cortexos.service import CortexService
from cortexos.vault import Vault


class StubAgent:
    async def run(self, prompt, skill_instructions="", backend=None, approved=False, request_id=None, on_output=None):
        return "Smoke-run placeholder deliverable. No external provider was called."


async def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="cortexos-smoke-") as temporary:
        root = Path(temporary)
        vault_path = root / "vault"
        settings = Settings(root=repo, vault=vault_path, skills=repo / "skills", backend="codex",
                            fast_model_enabled=False, providers={})
        vault = Vault(vault_path)
        vault.bootstrap()
        assert all((vault_path / folder).is_dir() for folder in ("raw", "wiki", "output", "requests", "receipts", "metrics"))
        assert json.loads((vault_path / "state.json").read_text(encoding="utf-8"))["version"] == 1

        service = CortexService(settings)
        service.agent = StubAgent()
        rules = await service.submit("list skills", request_id="smoke-tier1")
        assert rules["status"] == "completed" and rules["tier"] == 1

        skill_id = "content-editorial-outline"
        for number in range(5):
            result = await service.submit("Create a sample outline for smoke validation", skill_id=skill_id,
                                          request_id=f"smoke-skill-{number}")
            assert result["status"] == "completed"
            vote = service.feedback(result["id"], skill_id, True)
            assert vote["status"] == "saved"
        assert vault.skill_stats(skill_id)["successful_runs"] == 5
        vault.promote_skill(skill_id)
        vault.approve_promotion(skill_id)

        (vault_path / "automations.toml").write_text(
            '[[jobs]]\nid = "smoke-job"\nenabled = true\nskill_id = "content-editorial-outline"\n'
            'interval_minutes = 60\nprompt = "Create a sample outline for smoke validation"\n', encoding="utf-8")
        candidates = await scheduler_tick(service, dry_run=True, now_epoch=1_800_000_000)
        assert len(candidates) == 1 and candidates[0]["id"] == "smoke-job"

        held = await service.submit("Do an unclassified Tier 3 task", request_id="smoke-pending")
        assert held["status"] == "awaiting_approval"
        restarted = CortexService(settings)
        assert "smoke-pending" in restarted.pending
        assert restarted.reject("smoke-pending")["status"] == "rejected"
        print("CortexOS noninteractive smoke checks passed (temporary vault; no network or real metrics).")


if __name__ == "__main__":
    asyncio.run(main())
