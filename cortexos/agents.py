"""CortexOS multi-agent system.

12 specialized agents + a coordinator. Local-only (no server hop).
Agents = skills with a declared role. Coordinator routes via jev.
Persistent memory = vault. Scheduler = existing. Recovery = systemd.
Monitoring = /api/health. Secure tool access = approval gate.

Lazy notes:
- Agents reuse the Skill machinery (SkillRegistry, vault receipts, promotion).
- No new framework: agents are data (skill.json manifests), the coordinator
  is a jev-routed dispatch on top of the existing service.submit().
- Monitoring/health/backups are small modules, not platforms.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .skills import Skill, SkillRegistry
from .vault import Vault

logger = logging.getLogger("cortexos.agents")

# The 12 specialized agents. Each maps to an existing or new skill manifest.
# role: how the coordinator treats it. lane: which tier/model it prefers.
AGENT_SPECS: list[dict[str, Any]] = [
    {"id": "research-morning-intel", "role": "research", "lane": "cloud", "autonomous": True},
    {"id": "research-decision-brief", "role": "research", "lane": "cloud", "autonomous": True},
    {"id": "content-editorial-outline", "role": "content", "lane": "cloud", "autonomous": True},
    {"id": "memory-organize-capture", "role": "memory", "lane": "local", "autonomous": True},
    {"id": "productivity-weekly-review", "role": "productivity", "lane": "local", "autonomous": True},
    {"id": "community-pulse", "role": "community", "lane": "cloud", "autonomous": True},
    {"id": "client-meeting-brief", "role": "clients", "lane": "cloud", "autonomous": False},
    {"id": "sales-opportunity-brief", "role": "sales", "lane": "cloud", "autonomous": False},
    {"id": "finance-monthly-review", "role": "finance", "lane": "local", "autonomous": False},
    {"id": "automation-readiness", "role": "automation", "lane": "local", "autonomous": True},
    {"id": "system-health", "role": "ops", "lane": "local", "autonomous": True},
    {"id": "backup-restore", "role": "ops", "lane": "local", "autonomous": False},
]


@dataclass
class Agent:
    id: str
    name: str
    role: str
    lane: str  # local | cloud
    autonomous: bool  # may run without per-run approval
    skill: Skill | None = None
    runs: int = 0
    errors: int = 0
    last_run: str | None = None

    @property
    def health(self) -> str:
        if self.errors and self.runs and self.errors / self.runs > 0.5:
            return "degraded"
        return "ok"


class AgentCoordinator:
    """Routes prompts to the right agent; tracks per-agent health.

    The coordinator is a thin dispatcher: jev picks the role, the role maps to
    an agent, the agent is a skill executed by the existing service. No new
    execution engine.
    """

    ROLES = sorted({spec["role"] for spec in AGENT_SPECS})

    def __init__(self, registry: SkillRegistry, vault: Vault, jev=None):
        self.registry = registry
        self.vault = vault
        self.jev = jev
        self.agents: dict[str, Agent] = {}
        self._build_agents()

    def _build_agents(self) -> None:
        for spec in AGENT_SPECS:
            skill = self.registry.get(spec["id"])
            if skill is None:
                logger.warning("agent skill missing: %s", spec["id"])
                continue
            self.agents[spec["id"]] = Agent(
                id=spec["id"], name=skill.name, role=spec["role"],
                lane=spec["lane"], autonomous=spec["autonomous"], skill=skill,
            )

    async def route(self, prompt: str) -> Agent | None:
        """Pick an agent for this prompt. Jev classifies the role; falls back to trigger match."""
        decision = None
        if self.jev is not None:
            decision = await self.jev.decide("role", prompt)
        if decision and decision in self.agents_by_role():
            return self.agents_by_role()[decision]
        # fallback: skill trigger match (existing machinery)
        skill = self.registry.match(prompt)
        if skill and skill.id in self.agents:
            return self.agents[skill.id]
        return None

    def agents_by_role(self) -> dict[str, Agent]:
        return {agent.role: agent for agent in self.agents.values()}

    def status(self) -> dict:
        return {
            "agents": len(self.agents),
            "roles": {role: [a.id for a in self.agents.values() if a.role == role] for role in self.ROLES},
            "health": {a.id: a.health for a in self.agents.values()},
        }
