"""Explainable, bounded suggestions computed from real system activity.

Every suggestion references the evidence that produced it (queue task ids,
skill run counts, scheduled jobs) and explains why it appeared. Suggestions
never execute anything: acting on one is an explicit, separately validated API
call, and permission/automation changes always stay behind the existing
promotion and approval gates. Evaluation is bounded and cached; user decisions
(dismiss/snooze) persist under ``state/`` with 0600 permissions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import tomllib
from pathlib import Path

MAX_ITEMS = 20
PER_KIND_LIMIT = 5
DECISION_LIMIT = 200
DEFAULT_SNOOZE_SECONDS = 24 * 3600
CACHE_TTL_SECONDS = 10.0
ID_PATTERN = re.compile(r"^sg-[0-9a-f]{10}$")

KIND_ORDER = {
    "failed_task_recovery": 0,
    "scheduled_job": 1,
    "automation_stale": 2,
    "automation_candidate": 3,
    "repeated_workflow": 4,
    "reliability_review": 5,
}


def suggestion_id(kind: str, subject: str) -> str:
    digest = hashlib.sha256(f"{kind}:{subject}".encode("utf-8")).hexdigest()
    return f"sg-{digest[:10]}"


def normalize_prompt(text: str) -> str:
    """Group near-identical requests: lowercase, digits dropped, punctuation stripped."""
    lowered = re.sub(r"\d+", "", (text or "").lower())
    cleaned = re.sub(r"[^a-z]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


class SuggestionEngine:
    def __init__(self, root: Path):
        self.root = root
        self._cache: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------- decisions

    @property
    def state_path(self) -> Path:
        return self.root / "suggestions.json"

    def _decisions(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, dict) else {}

    def _write_decisions(self, items: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "items": items}, indent=2, sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix=".suggestions-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _decide(self, subject_id: str, status: str, *, snooze_seconds: float | None = None) -> dict:
        if not isinstance(subject_id, str) or not ID_PATTERN.match(subject_id):
            raise ValueError("Invalid suggestion id")
        items = self._decisions()
        entry = {"status": status, "at": time.time()}
        if snooze_seconds:
            entry["until"] = time.time() + snooze_seconds
        items[subject_id] = entry
        if len(items) > DECISION_LIMIT:
            ordered = sorted(items.items(), key=lambda pair: pair[1].get("at", 0))
            items = dict(ordered[-DECISION_LIMIT:])
        self._write_decisions(items)
        self._cache = None
        return {"id": subject_id, **entry}

    def dismiss(self, subject_id: str) -> dict:
        return self._decide(subject_id, "dismissed")

    def snooze(self, subject_id: str, seconds: float = DEFAULT_SNOOZE_SECONDS) -> dict:
        return self._decide(subject_id, "snoozed", snooze_seconds=seconds)

    # ------------------------------------------------------------ evaluation

    def evaluate(self, service, *, now: float | None = None, use_cache: bool = True) -> dict:
        now = float(now or time.time())
        cached = self._cache
        if use_cache and cached and now - cached[0] < CACHE_TTL_SECONDS:
            generated = cached[1]
        else:
            generated = self._generate(service, now)
            self._cache = (now, generated)
        decisions = self._decisions()
        visible = []
        for item in generated:
            decision = decisions.get(item["id"]) or {}
            status = decision.get("status")
            if status == "dismissed":
                continue
            if status == "snoozed" and now < float(decision.get("until", 0) or 0):
                continue
            visible.append({**item, "decision": "active"})
        visible = visible[:MAX_ITEMS]
        counts: dict[str, int] = {}
        for item in visible:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return {"generated_at": now, "total": len(visible), "counts": counts, "items": visible}

    @staticmethod
    def find(report: dict, subject_id: str) -> dict | None:
        return next((item for item in report.get("items", []) if item["id"] == subject_id), None)

    # ------------------------------------------------------------ generators

    def _generate(self, service, now: float) -> list[dict]:
        skills = service.registry.list()
        index = service._receipt_index_cached()
        items: list[dict] = []
        items.extend(self._failed_tasks(service, now))
        items.extend(self._scheduled_jobs(service, now))
        items.extend(self._skill_items(service, now, skills, index))
        items.extend(self._repeated_workflows(service, now))
        items.sort(key=lambda item: (KIND_ORDER.get(item["kind"], 9), -(item.get("created_at") or 0)))
        return items

    @staticmethod
    def _failed_tasks(service, now: float) -> list[dict]:
        queue = getattr(service, "queue", None)
        if queue is None:
            return []
        try:
            tasks = queue.store.list(status="failed", limit=PER_KIND_LIMIT * 3)
        except Exception:
            return []
        items = []
        for task in tasks:
            error = str(task.get("error") or "unknown error")[:200]
            prompt = str(task.get("prompt") or "")[:60]
            items.append({
                "id": suggestion_id("failed_task_recovery", str(task["id"])),
                "kind": "failed_task_recovery",
                "title": f"Recover failed task: {prompt}",
                "why": (f"Queue task failed after {task.get('attempts')} of {task.get('max_attempts')} attempts: "
                        f"{error}. Retrying is your decision; nothing runs automatically."),
                "evidence": [{"type": "task", "id": task["id"],
                              "detail": f"status=failed attempts={task.get('attempts')} error={error}"}],
                "actions": [{"action": "retry_task", "label": "Retry task", "ref": f"task:{task['id']}"},
                            {"action": "inspect", "label": "Inspect task", "ref": f"task:{task['id']}"}],
                "created_at": now,
            })
            if len(items) >= PER_KIND_LIMIT:
                break
        return items

    @staticmethod
    def _scheduled_jobs(service, now: float) -> list[dict]:
        config_path = service.settings.vault / "automations.toml"
        if not config_path.exists():
            return []
        try:
            jobs = tomllib.loads(config_path.read_text(encoding="utf-8")).get("jobs", [])
        except (OSError, ValueError):
            return []
        items = []
        for index, job in enumerate(jobs):
            skill_id = str(job.get("skill_id", ""))
            if not job.get("enabled", False) or not skill_id:
                continue
            job_id = str(job.get("id") or f"{skill_id}-{index}")
            skill = service.registry.get(skill_id)
            evidence = [{"type": "job", "id": job_id,
                         "detail": f"skill={skill_id} interval_minutes={job.get('interval_minutes')}"}]
            actions = [{"action": "inspect", "label": "Inspect skill", "ref": f"skill:{skill_id}"}]
            if not skill:
                why = f"Skill '{skill_id}' is not in the registry, so this scheduled job will never run."
            else:
                permitted, reason = service.automation_permitted(skill)
                if permitted:
                    continue
                why = f"Job '{job_id}' is enabled but CortexOS will not run it: {reason}."
                stats = service.skill_stats(skill.id, skill=skill)
                evidence.append({"type": "skill", "id": skill.id,
                                 "detail": f"runs={stats['successful_runs']} promotion={stats['promotion_status']}"})
                if stats["promotion_status"] != "pending_review":
                    available, gate_reason = SuggestionEngine._review_readiness(skill, stats)
                    actions.insert(0, {"action": "request_review", "label": "Request automation review",
                                       "ref": f"skill:{skill.id}", "available": available,
                                       **({"reason": gate_reason} if gate_reason else {})})
            items.append({
                "id": suggestion_id("scheduled_job", job_id),
                "kind": "scheduled_job",
                "title": f"Scheduled job '{job_id}' cannot run yet",
                "why": why,
                "evidence": evidence,
                "actions": actions,
                "created_at": now,
            })
            if len(items) >= PER_KIND_LIMIT:
                break
        return items

    @staticmethod
    def _review_readiness(skill, stats: dict) -> tuple[bool, str | None]:
        if stats["successful_runs"] < 5:
            return False, "Five user-confirmed successful runs are required for the current version"
        test = (stats.get("evidence") or {}).get("test")
        if skill.tests and (not test or test.get("status") != "passed"):
            return False, "Declared tests must pass for the current version"
        return True, None

    def _skill_items(self, service, now: float, skills: list, index: dict) -> list[dict]:
        items: list[dict] = []
        candidates = stale = reliability = 0
        for skill in skills:
            if skill.id not in index:
                continue
            stats = service.skill_stats(skill.id, skill=skill)
            if stats["promotion_status"] == "approved":
                permitted, reason = service.automation_permitted(skill)
                if permitted or stale >= PER_KIND_LIMIT:
                    continue
                available, gate_reason = self._review_readiness(skill, stats)
                items.append({
                    "id": suggestion_id("automation_stale", skill.id),
                    "kind": "automation_stale",
                    "title": f"{skill.name}: automation approval is stale",
                    "why": f"The approved automation review no longer matches this skill version: {reason}.",
                    "evidence": [{"type": "skill", "id": skill.id,
                                  "detail": f"approved_hash vs current hash={skill.content_hash[:23]} runs={stats['successful_runs']}"}],
                    "actions": [{"action": "request_review", "label": "Request a new automation review",
                                 "ref": f"skill:{skill.id}", "available": available,
                                 **({"reason": gate_reason} if gate_reason else {})},
                                {"action": "inspect", "label": "Inspect skill", "ref": f"skill:{skill.id}"}],
                    "created_at": now,
                })
                stale += 1
                continue
            if stats["promotion_status"] == "pending_review":
                continue
            if stats["successful_runs"] >= 5 and candidates < PER_KIND_LIMIT:
                available, gate_reason = self._review_readiness(skill, stats)
                items.append({
                    "id": suggestion_id("automation_candidate", skill.id),
                    "kind": "automation_candidate",
                    "title": f"{skill.name} has {stats['successful_runs']} confirmed runs",
                    "why": ("Five or more user-confirmed successful runs make this skill eligible for a manual "
                            "automation review; CortexOS never enables unattended runs automatically."),
                    "evidence": [{"type": "skill", "id": skill.id,
                                  "detail": f"successful={stats['successful_runs']} errors={stats['error_runs']} hash={skill.content_hash[:23]}"}],
                    "actions": [{"action": "request_review", "label": "Request automation review",
                                 "ref": f"skill:{skill.id}", "available": available,
                                 **({"reason": gate_reason} if gate_reason else {})},
                                {"action": "inspect", "label": "Inspect skill", "ref": f"skill:{skill.id}"}],
                    "created_at": now,
                })
                candidates += 1
            if stats["error_runs"] >= 2 and reliability < PER_KIND_LIMIT:
                items.append({
                    "id": suggestion_id("reliability_review", skill.id),
                    "kind": "reliability_review",
                    "title": f"{skill.name} has {stats['error_runs']} failed runs",
                    "why": ("Repeated user-marked failures suggest the skill or its inputs need review "
                            "before it is trusted further."),
                    "evidence": [{"type": "skill", "id": skill.id,
                                  "detail": f"errors={stats['error_runs']} successful={stats['successful_runs']}"}],
                    "actions": [{"action": "inspect", "label": "Inspect skill", "ref": f"skill:{skill.id}"}],
                    "created_at": now,
                })
                reliability += 1
        return items

    @staticmethod
    def _repeated_workflows(service, now: float) -> list[dict]:
        queue = getattr(service, "queue", None)
        if queue is None:
            return []
        try:
            tasks = queue.store.list(status="completed", limit=500)
        except Exception:
            return []
        groups: dict[str, list[dict]] = {}
        for task in tasks:
            prompt = str(task.get("prompt") or "").strip()
            if len(prompt) < 12:
                continue
            key = normalize_prompt(prompt)
            if len(key) < 10:
                continue
            groups.setdefault(key, []).append(task)
        items = []
        for key, group in sorted(groups.items(), key=lambda pair: -len(pair[1])):
            if len(group) < 3:
                continue
            prompt = str(group[0].get("prompt") or "")
            if service.registry.match(prompt):
                continue  # already covered by a skill; creating skills is a human decision
            items.append({
                "id": suggestion_id("repeated_workflow", key[:120]),
                "kind": "repeated_workflow",
                "title": f"Repeated request x{len(group)}: {prompt[:60]}",
                "why": (f"This request completed {len(group)} times. A skill could make it repeatable; "
                        "CortexOS will not create or enable one on its own."),
                "evidence": [{"type": "task", "id": task["id"], "detail": "completed"} for task in group[:3]],
                "actions": [{"action": "inspect", "label": "Inspect tasks", "ref": f"task:{group[0]['id']}"}],
                "created_at": now,
            })
            if len(items) >= PER_KIND_LIMIT:
                break
        return items
