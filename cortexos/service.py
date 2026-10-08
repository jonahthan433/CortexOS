from __future__ import annotations

import uuid

from .config import Settings
from .providers import AgentProvider, FastModel
from .router import Router
from .skills import SkillRegistry
from .vault import Vault


class CortexService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.registry = SkillRegistry(settings.skills)
        self.router = Router(self.registry)
        self.vault = Vault(settings.vault)
        self.agent = AgentProvider(settings)
        self.fast_model = FastModel(settings)
        self.pending: dict[str, dict] = self.vault.load_state().get("pending_approvals", {})

    def status(self) -> dict:
        return {"name": "CortexOS", "state": "Idle", "backend": self.settings.backend,
                "vault": str(self.settings.vault), "skills": len(self.registry.list()), "voice": "disabled"}

    async def submit(self, prompt: str, backend: str | None = None, skill_id: str | None = None,
                     request_id: str | None = None, on_output=None) -> dict:
        request_id = request_id or str(uuid.uuid4())
        if self.vault.request_exists(request_id):
            return {"id": request_id, "status": "error", "error": "Request ID has already been used; submit a new request instead."}
        route = self.router.route(prompt)
        if skill_id:
            skill = self.registry.get(skill_id)
            if not skill:
                return {"id": request_id, "status": "error", "error": f"Skill not found: {skill_id}"}
            route = type(route)(skill.tier, skill, f"explicit skill: {skill.id}")
        else:
            skill = route.skill
        self.vault.log_request(request_id, prompt, "received", route.tier, backend or self.settings.backend)

        # Enforce the action gate before every tier, including local rule lookups.
        decision = {"risk_level": skill.risk_level, "requires_approval": skill.approval_required or skill.risk_level in {"external_action", "financial", "unknown"},
                    "reason": "Declared in skill manifest"} if skill else None
        if route.tier == 3 and decision is None:
            decision = await self.fast_model.classify_action(prompt)
        if decision and decision["requires_approval"]:
            pending = {"prompt": prompt, "backend": backend, "skill_id": skill.id if skill else None,
                       "risk_level": decision["risk_level"], "reason": decision["reason"]}
            self.pending[request_id] = pending
            self.vault.save_pending(request_id, pending)
            self.vault.log_request(request_id, prompt, "awaiting_approval", route.tier, backend or self.settings.backend)
            self.vault.receipt(f"{request_id}-approval-pending", {
                "event": "approval_request", "request_id": request_id, "status": "awaiting_approval",
                "skill": skill.id if skill else None, "risk_level": decision["risk_level"],
                "reason": decision["reason"],
            })
            return {"id": request_id, "status": "awaiting_approval", "message": "This request requires explicit human approval before execution.", "skill": skill.id if skill else None,
                    "risk_level": decision["risk_level"], "reason": decision["reason"]}

        if route.tier == 1:
            if prompt.strip().lower() in {"help", "list skills", "show skills"} or prompt.strip().lower().startswith("list skills"):
                result = "\n".join(f"- **{s.name}** (`{s.id}`): {s.description}" for s in self.registry.list()) or "No skills found."
            elif prompt.strip().lower().startswith("metric "):
                key = prompt.strip()[7:].strip().lower()
                files = sorted((p for p in (self.settings.vault / "metrics").glob("**/*") if p.is_file() and p.stem.lower() == key), key=lambda p: p.suffix != ".toml")
                result = files[0].read_text(encoding="utf-8")[:12000] if files else f"No metric file named '{key}' was found under metrics/."
            elif prompt.strip().lower() in {"show metrics", "list metrics"}:
                files = sorted(p for p in (self.settings.vault / "metrics").glob("**/*") if p.is_file())
                result = "\n".join(f"- {p.relative_to(self.settings.vault)}" for p in files) or "No metric files yet. Add only real, user-provided numbers under metrics/."
            else:
                result = str(self.status())
            return self._finish(request_id, prompt, result, "completed", 1, backend, skill)

        if route.tier == 2:
            if not self.settings.fast_model_enabled:
                route = type(route)(3, skill, "tier 2 disabled; falling back to selected agent backend")
        if route.tier == 2:
            try:
                result = await self.fast_model.answer(prompt)
                return self._finish(request_id, prompt, result, "completed", 2, backend, skill)
            except Exception as exc:
                # Fallback is visible and uses the selected agent backend.
                route = type(route)(3, skill, f"tier 2 unavailable: {exc}")

        if route.tier == 3 and not skill and self.settings.fast_model_enabled:
            try:
                if await self.fast_model.classify(prompt) == "QUICK":
                    result = await self.fast_model.answer(prompt)
                    return self._finish(request_id, prompt, result, "completed", 2, backend, skill)
            except Exception:
                pass  # Conservative fallback: continue to the selected agent backend.

        if route.tier == 3 or (skill and skill.tier == 3):
            return await self._execute(request_id, prompt, backend, skill, on_output=on_output)
        return self._finish(request_id, prompt, "No execution path matched.", "completed", route.tier, backend, skill)

    async def approve(self, request_id: str, on_output=None) -> dict:
        pending = self.pending.pop(request_id, None)
        if not pending:
            return {"id": request_id, "status": "error", "error": "No pending approval for this request."}
        self.vault.remove_pending(request_id)
        skill = self.registry.get(pending["skill_id"]) if pending.get("skill_id") else None
        return await self._execute(request_id, pending["prompt"], pending.get("backend"), skill, approved=True,
                                    on_output=on_output)

    def reject(self, request_id: str) -> dict:
        pending = self.pending.pop(request_id, None)
        if not pending:
            return {"id": request_id, "status": "error", "error": "No pending approval for this request."}
        self.vault.remove_pending(request_id)
        self.vault.log_request(request_id, pending["prompt"], "rejected", backend=pending.get("backend"))
        self.vault.receipt(request_id, {"event": "approval_decision", "request_id": request_id,
                                        "status": "rejected", "skill": pending.get("skill_id")})
        return {"id": request_id, "status": "rejected"}

    async def _execute(self, request_id: str, prompt: str, backend: str | None, skill, approved: bool = False,
                       on_output=None) -> dict:
        try:
            result = await self.agent.run(prompt, skill.instructions if skill else "", backend, approved=approved,
                                          request_id=request_id, on_output=on_output)
            return self._finish(request_id, prompt, result, "completed", 3, backend, skill)
        except Exception as exc:
            self.vault.log_request(request_id, prompt, "error", 3, backend or self.settings.backend)
            self.vault.receipt(request_id, {"status": "error", "error": str(exc), "skill": skill.id if skill else None})
            return {"id": request_id, "status": "error", "error": str(exc)}

    def _finish(self, request_id: str, prompt: str, result: str, status: str, tier: int, backend: str | None, skill) -> dict:
        output = self.vault.write_result(request_id, prompt, result, skill.id if skill else None)
        self.vault.log_request(request_id, prompt, status, tier, backend or self.settings.backend, str(output))
        receipt = self.vault.receipt(request_id, {"request_id": request_id, "status": status, "tier": tier, "backend": backend or self.settings.backend,
                                                     "skill": skill.id if skill else None, "output": str(output)})
        return {"id": request_id, "status": status, "tier": tier, "backend": backend or self.settings.backend,
                "skill": skill.id if skill else None, "result": result, "output": str(output), "receipt": str(receipt)}

    def feedback(self, request_id: str, skill_id: str, useful: bool) -> dict:
        if not self.registry.get(skill_id):
            return {"status": "error", "error": f"Skill not found: {skill_id}"}
        try:
            path = self.vault.skill_feedback(request_id, skill_id, useful)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "saved", "receipt": str(path), "skill": skill_id}
