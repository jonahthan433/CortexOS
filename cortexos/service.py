from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from .config import Settings
from .llm import ModelGateway
from .providers import AgentProvider, FastModel
from .router import Router
from .skills import Skill, SkillHistory, SkillRegistry, declared_gate
from .vault import Vault


class CortexService:
    RECEIPT_INDEX_TTL_SECONDS = 5.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self.registry = SkillRegistry(settings.skills)
        self.router = Router(self.registry)
        self.vault = Vault(settings.vault)
        self.agent = AgentProvider(settings)
        self.gateway = ModelGateway(settings)
        self.fast_model = FastModel(settings, gateway=self.gateway)
        self.history = SkillHistory(settings.root / "state" / "skill_history")
        self._receipt_index: tuple[float, dict] | None = None
        self.pending: dict[str, dict] = self.vault.load_state().get("pending_approvals", {})
        self.queue = None  # attached by the Bridge once the durable queue starts
        self.sync_history()

    def status(self) -> dict:
        return {"name": "CortexOS", "state": "Idle", "backend": self.settings.backend,
                "vault": str(self.settings.vault), "skills": len(self.registry.list()), "voice": "disabled",
                "llm": {"providers": len(self.gateway.providers), "assigned_agents": len(self.gateway.agent_ids)}}

    # ------------------------------------------------------------------ skills

    def sync_history(self) -> dict:
        """Snapshot first-seen and changed skills into the version history store."""
        recorded = self.history.sync(self.registry.list())
        return {"recorded": recorded}

    def _receipt_index_cached(self) -> dict:
        cached = self._receipt_index
        if cached and time.time() - cached[0] < self.RECEIPT_INDEX_TTL_SECONDS:
            return cached[1]
        index = self.vault.receipt_index()
        self._receipt_index = (time.time(), index)
        return index

    def skill_stats(self, skill_id: str, skill: Skill | None = None) -> dict:
        skill = skill or self.registry.get(skill_id)
        if not skill:
            return {"successful_runs": 0, "error_runs": 0, "automation_review_due": False,
                    "promotion_status": "not_started", "recent_runs": [],
                    "automation_permitted": False, "automation_permitted_reason": "skill not found"}
        stats = self.vault.skill_stats(skill_id, current_hash=skill.content_hash,
                                       baseline_hash=self.history.baseline_hash(skill_id),
                                       index=self._receipt_index_cached())
        stats["version"] = skill.version
        stats["version_source"] = skill.version_source
        stats["content_hash"] = skill.content_hash
        permitted, reason = self.automation_permitted(skill)
        stats["automation_permitted"] = permitted
        stats["automation_permitted_reason"] = reason
        return stats

    def automation_permitted(self, skill: Skill) -> tuple[bool, str]:
        """True only for an approved promotion that matches the current skill version."""
        promotion = self.vault.promotion(skill.id)
        if promotion.get("status") != "approved":
            return False, f"promotion is {promotion.get('status', 'not_started')}"
        recorded = promotion.get("skill_hash")
        if recorded:
            if recorded != skill.content_hash:
                return False, "skill changed since automation approval; a new review is required"
            return True, "approved promotion matches this version"
        baseline = self.history.baseline_hash(skill.id)
        if baseline and baseline != skill.content_hash:
            return False, "skill changed since automation approval; a new review is required"
        return True, "legacy approval without a recorded version"

    def request_promotion(self, skill_id: str) -> dict:
        skill = self.registry.get(skill_id)
        if not skill:
            raise ValueError("Skill not found")
        self.sync_history()
        skill = self.registry.get(skill_id) or skill
        stats = self.skill_stats(skill_id, skill=skill)
        if stats["successful_runs"] < 5:
            raise ValueError("Five user-confirmed successful runs are required")
        test = (stats.get("evidence") or {}).get("test")
        if skill.tests and (not test or test.get("status") != "passed"):
            raise ValueError("Declared tests must pass for the current version before automation review")
        return self.vault.promote_skill(skill_id, stats=stats, skill_hash=skill.content_hash)

    def approve_promotion(self, skill_id: str) -> dict:
        skill = self.registry.get(skill_id)
        if not skill:
            raise ValueError("Skill not found")
        current = self.vault.promotion(skill_id)
        if current.get("skill_hash") and current["skill_hash"] != skill.content_hash:
            raise ValueError("Skill changed since the review started; request a new review")
        return self.vault.approve_promotion(skill_id, skill_hash=skill.content_hash)

    def run_skill_tests(self, skill_id: str) -> dict:
        """Run declared tests deterministically: routing, tier, and gate decisions only.

        Tests never call a provider or perform side effects, so they are
        repeatable and cheap. A passing test receipt is separate evidence from
        user-confirmed run feedback and never grants automation by itself.
        """
        skill = self.registry.get(skill_id)
        if not skill:
            raise ValueError("Skill not found")
        if not skill.tests:
            raise ValueError("No tests are declared in this skill manifest")
        cases = []
        for case in skill.tests:
            route = self.router.route(case["prompt"])
            decision = declared_gate(route.skill)
            actual = {"matched_skill": route.skill.id if route.skill else None,
                      "tier": route.tier,
                      "requires_approval": bool(decision and decision["requires_approval"])}
            expect = case["expect"]
            mismatches = []
            if "skill_id" in expect and actual["matched_skill"] != expect["skill_id"]:
                mismatches.append(f"expected skill '{expect['skill_id']}', matched {actual['matched_skill']!r}")
            if "tier" in expect and actual["tier"] != expect["tier"]:
                mismatches.append(f"expected tier {expect['tier']}, routed to tier {actual['tier']}")
            if "requires_approval" in expect and actual["requires_approval"] != expect["requires_approval"]:
                mismatches.append(f"expected requires_approval={expect['requires_approval']}, "
                                  f"decision was {actual['requires_approval']}")
            cases.append({"name": case["name"], "prompt": case["prompt"],
                          "expected": expect, "actual": actual,
                          "passed": not mismatches, "mismatches": mismatches})
        passed = sum(1 for case in cases if case["passed"])
        payload = {"event": "skill_test", "skill": skill.id, "skill_version": skill.version,
                   "skill_hash": skill.content_hash, "mode": "routing",
                   "status": "passed" if passed == len(cases) else "failed",
                   "total": len(cases), "passed": passed, "cases": cases}
        request_id = f"skilltest-{skill.id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        receipt = self.vault.receipt(request_id, payload)
        self._receipt_index = None
        return {"skill": skill.id, "hash": skill.content_hash, "version": skill.version,
                "status": payload["status"], "total": len(cases), "passed": passed,
                "cases": cases, "receipt": str(receipt)}

    def history_view(self, skill_id: str) -> dict:
        skill = self.registry.get(skill_id)
        if not skill:
            raise ValueError("Skill not found")
        self.sync_history()
        skill = self.registry.get(skill_id) or skill
        entries = self.history.entries(skill_id)
        return {"skill": skill_id,
                "current": {"hash": skill.content_hash, "version": skill.version,
                            "version_source": skill.version_source},
                "baseline_hash": entries[0].get("hash") if entries else None,
                "entries": [{"hash": item.get("hash"), "version": item.get("version"),
                             "version_source": item.get("version_source"), "at": item.get("at"),
                             "source": item.get("source"), "rolled_back_from": item.get("rolled_back_from"),
                             "current": item.get("hash") == skill.content_hash}
                            for item in reversed(entries)]}

    def history_entry(self, skill_id: str, target_hash: str) -> dict:
        entry = self.history.entry(skill_id, target_hash)
        if not entry:
            raise ValueError("Unknown version for this skill")
        return {"skill": skill_id, "hash": entry.get("hash"), "version": entry.get("version"),
                "version_source": entry.get("version_source"), "at": entry.get("at"),
                "source": entry.get("source"), "instructions_name": entry.get("instructions_name"),
                "instructions": (entry.get("instructions") or "")[:8000],
                "manifest": entry.get("manifest")}

    def rollback_skill(self, skill_id: str, target_hash: str, confirm: bool = False) -> dict:
        skill = self.registry.get(skill_id)
        if not skill:
            raise ValueError("Skill not found")
        if not confirm:
            raise ValueError("Rollback requires explicit confirmation")
        if target_hash == skill.content_hash:
            raise ValueError("That version is already current")
        self.sync_history()
        skill = self.registry.get(skill_id) or skill
        entry = self.history.entry(skill_id, target_hash)
        if not entry:
            raise ValueError("Unknown version for this skill")
        self.history.rollback(skill, entry)
        self.history.record_rollback(skill_id, entry, from_hash=skill.content_hash)
        receipt = self.vault.receipt(f"skillrollback-{skill_id}-{int(time.time())}", {
            "event": "skill_rollback", "skill": skill_id, "status": "completed",
            "from_hash": skill.content_hash, "to_hash": target_hash,
            "from_version": skill.version, "to_version": entry.get("version")})
        self._receipt_index = None
        return {"skill": skill_id, "status": "rolled_back", "to_hash": target_hash,
                "to_version": entry.get("version"), "receipt": str(receipt)}

    # ---------------------------------------------------------------- requests

    @staticmethod
    def _iso(epoch: float | None) -> str | None:
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")

    def _receipt_context(self, request_id: str, task_id: str | None, skill: Skill | None,
                         started: float | None) -> dict:
        finished = time.time()
        return {"event": "execution", "request_id": request_id, "task_id": task_id,
                "skill": skill.id if skill else None,
                "skill_version": skill.version if skill else None,
                "skill_hash": skill.content_hash if skill else None,
                "started_at": self._iso(started), "finished_at": self._iso(finished),
                "duration_ms": int((finished - started) * 1000) if started else None}

    async def submit(self, prompt: str, backend: str | None = None, skill_id: str | None = None,
                     request_id: str | None = None, on_output=None, task_id: str | None = None) -> dict:
        request_id = request_id or str(uuid.uuid4())
        started = time.time()
        if self.vault.request_exists(request_id):
            return {"id": request_id, "status": "error", "error": "Request ID has already been used; submit a new request instead."}
        route = self.router.route(prompt)
        known_backends = list(self.settings.agent_ids) + self.gateway.agent_ids
        if backend and backend not in known_backends:
            return {"id": request_id, "status": "error", "error": f"Unknown agent: {backend}"}
        if skill_id:
            skill = self.registry.get(skill_id)
            if not skill:
                return {"id": request_id, "status": "error", "error": f"Skill not found: {skill_id}"}
            route = type(route)(skill.tier, skill, f"explicit skill: {skill.id}")
        else:
            skill = route.skill
        # Explicit agent selection overrides model routing for work, but local
        # Tier 1 commands remain instant and deterministic.
        if backend and route.tier != 1:
            route = type(route)(3, skill, f"explicit agent: {backend}")
        self.vault.log_request(request_id, prompt, "received", route.tier, backend or self.settings.backend)

        # Enforce the action gate before every tier, including local rule lookups.
        decision = declared_gate(skill)
        tier_decision = None
        if decision is None and route.tier != 1:
            # Tier 1/2 are local reads and read-only answers; only agent work
            # (or an undecidable request) passes through the action gate.
            try:
                tier_decision = await self.fast_model.classify(prompt)
            except Exception:
                # An unavailable classifier must never break submission; work
                # continues conservatively and the risk gate still applies.
                tier_decision = "WORK"
            if tier_decision == "WORK":
                decision = await self.fast_model.classify_action(prompt)
        if decision and decision["requires_approval"]:
            pending = {"prompt": prompt, "backend": backend, "skill_id": skill.id if skill else None,
                       "risk_level": decision["risk_level"], "reason": decision["reason"],
                       "task_id": task_id, "created_at": time.time()}
            self.pending[request_id] = pending
            self.vault.save_pending(request_id, pending)
            self.vault.log_request(request_id, prompt, "awaiting_approval", route.tier, backend or self.settings.backend)
            self.vault.receipt(f"{request_id}-approval-pending", {
                "event": "approval_request", "request_id": request_id, "task_id": task_id,
                "status": "awaiting_approval",
                "skill": skill.id if skill else None, "risk_level": decision["risk_level"],
                "skill_version": skill.version if skill else None,
                "skill_hash": skill.content_hash if skill else None,
                "reason": decision["reason"],
            })
            return {"id": request_id, "status": "awaiting_approval", "message": "This request requires explicit human approval before execution.", "skill": skill.id if skill else None,
                    "risk_level": decision["risk_level"], "reason": decision["reason"],
                    "route_reason": route.reason}

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
            return self._finish(request_id, prompt, result, "completed", 1, backend, skill, reason=route.reason,
                                started=started, task_id=task_id)

        if route.tier == 2:
            if not self.settings.fast_model_enabled:
                route = type(route)(3, skill, "tier 2 disabled; falling back to selected agent backend")
        if route.tier == 2:
            try:
                result = await self.fast_model.answer(prompt)
                return self._finish(request_id, prompt, result, "completed", 2, backend, skill, reason=route.reason,
                                    started=started, task_id=task_id)
            except Exception as exc:
                # Fallback is visible and uses the selected agent backend.
                route = type(route)(3, skill, f"tier 2 unavailable: {exc}")

        if route.tier == 3 and not skill and backend is None:
            # Jev routes tier1_local here (prompt wasn't a literal command): demote to the
            # tier-1 local handler. Tier 2 answering still requires a configured fast model.
            # Reuse the gate-phase verdict; a second call can disagree (nondeterministic jev).
            if tier_decision == "LOCAL":
                return self._finish(request_id, prompt, str(self.status()), "completed", 1, backend, skill, reason=route.reason,
                                    started=started, task_id=task_id)
            if tier_decision == "QUICK" and self.settings.fast_model_enabled:
                try:
                    result = await self.fast_model.answer(prompt)
                    return self._finish(request_id, prompt, result, "completed", 2, backend, skill, reason=route.reason,
                                        started=started, task_id=task_id)
                except Exception:
                    pass  # Conservative fallback: continue to the selected agent backend.

        if route.tier == 3 or (skill and skill.tier == 3):
            return await self._execute(request_id, prompt, backend, skill, on_output=on_output, reason=route.reason,
                                       started=started, task_id=task_id)
        return self._finish(request_id, prompt, "No execution path matched.", "completed", route.tier, backend, skill,
                            reason=route.reason, started=started, task_id=task_id)

    async def approve(self, request_id: str, on_output=None) -> dict:
        pending = self.pending.pop(request_id, None)
        if not pending:
            return {"id": request_id, "status": "error", "error": "No pending approval for this request."}
        self.vault.remove_pending(request_id)
        skill = self.registry.get(pending["skill_id"]) if pending.get("skill_id") else None
        return await self._execute(request_id, pending["prompt"], pending.get("backend"), skill, approved=True,
                                    on_output=on_output, reason="human-approved pending request",
                                    started=pending.get("created_at"), task_id=pending.get("task_id"))

    def reject(self, request_id: str) -> dict:
        pending = self.pending.pop(request_id, None)
        if not pending:
            return {"id": request_id, "status": "error", "error": "No pending approval for this request."}
        self.vault.remove_pending(request_id)
        self.vault.log_request(request_id, pending["prompt"], "rejected", backend=pending.get("backend"))
        self.vault.receipt(request_id, {"event": "approval_decision", "request_id": request_id,
                                        "task_id": pending.get("task_id"),
                                        "status": "rejected", "skill": pending.get("skill_id")})
        return {"id": request_id, "status": "rejected"}

    async def _execute(self, request_id: str, prompt: str, backend: str | None, skill, approved: bool = False,
                       on_output=None, reason: str | None = None, started: float | None = None,
                       task_id: str | None = None) -> dict:
        agent_id = backend or self.settings.backend
        assignment = self.gateway.resolve(agent_id)
        try:
            if assignment is not None:
                # Universal provider path: same system prompt, same approval
                # gate, different inference endpoint.
                outcome = await self.gateway.generate(agent_id, prompt,
                                                      skill_instructions=skill.instructions if skill else "",
                                                      approved=approved, request_id=request_id,
                                                      on_output=on_output)
                result = outcome.text
                extra = {"llm": outcome.meta()}
            else:
                result = await self.agent.run(prompt, skill.instructions if skill else "", backend, approved=approved,
                                              request_id=request_id, on_output=on_output)
                extra = None
            return self._finish(request_id, prompt, result, "completed", 3, backend, skill, reason=reason,
                                extra=extra, started=started, task_id=task_id)
        except Exception as exc:
            self.vault.log_request(request_id, prompt, "error", 3, backend or self.settings.backend)
            self.vault.receipt(request_id, {**self._receipt_context(request_id, task_id, skill, started),
                                            "status": "error", "error": str(exc)})
            self._receipt_index = None
            return {"id": request_id, "status": "error", "error": str(exc), "route_reason": reason,
                    "retryable": bool(getattr(exc, "retryable", False))}

    def _finish(self, request_id: str, prompt: str, result: str, status: str, tier: int, backend: str | None, skill,
                reason: str | None = None, extra: dict | None = None, started: float | None = None,
                task_id: str | None = None) -> dict:
        output = self.vault.write_result(request_id, prompt, result, skill.id if skill else None)
        self.vault.log_request(request_id, prompt, status, tier, backend or self.settings.backend, str(output))
        receipt = self.vault.receipt(request_id, {**self._receipt_context(request_id, task_id, skill, started),
                                                  "status": status, "tier": tier,
                                                  "backend": backend or self.settings.backend,
                                                  "output": str(output),
                                                  **({"llm": extra["llm"]} if extra else {})})
        self._receipt_index = None
        return {"id": request_id, "status": status, "tier": tier, "backend": backend or self.settings.backend,
                "skill": skill.id if skill else None, "result": result, "output": str(output), "receipt": str(receipt),
                "route_reason": reason, **({"llm": extra["llm"]} if extra else {})}

    def feedback(self, request_id: str, skill_id: str, useful: bool) -> dict:
        skill = self.registry.get(skill_id)
        if not skill:
            return {"status": "error", "error": f"Skill not found: {skill_id}"}
        try:
            path = self.vault.skill_feedback(request_id, skill_id, useful, skill_hash=skill.content_hash)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        self._receipt_index = None
        return {"status": "saved", "receipt": str(path), "skill": skill_id, "skill_hash": skill.content_hash}
