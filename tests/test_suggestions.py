"""Stage 7 suggestions tests: evidence-backed generation, bounded output,
dedup via stable ids, dismiss/snooze persistence, and action safety (nothing
executes during evaluation; actions re-validate against live state)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from cortexos.config import Settings
from cortexos.queue import QueueStore
from cortexos.service import CortexService
from cortexos.suggestions import normalize_prompt
from cortexos.vault import Vault


def write_skill(root: Path, skill_id: str = "demo", *, manifest: dict | None = None) -> Path:
    base = root / "automation" / skill_id
    base.mkdir(parents=True, exist_ok=True)
    data = {"id": skill_id, "name": "Demo Skill", "domain": "Automation", "description": "demo skill",
            "trigger_phrases": ["demo thing"], "tier": 1, "risk_level": "read_only",
            "approval_required": False, "automation_ready": False, "instructions": "SKILL.md"}
    if manifest:
        data.update(manifest)
    (base / "skill.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (base / "SKILL.md").write_text("Demo instructions.", encoding="utf-8")
    return base


def make_service(tmp_path: Path) -> CortexService:
    (tmp_path / "skills").mkdir(exist_ok=True)
    (tmp_path / "vault").mkdir(exist_ok=True)
    settings = Settings(root=tmp_path, vault=tmp_path / "vault", skills=tmp_path / "skills",
                        queue_db=tmp_path / "state" / "queue.db")
    return CortexService(settings)


def feedback_receipts(vault: Vault, skill_id: str, count: int, *, skill_hash: str | None, useful: bool = True) -> None:
    for index in range(count):
        request_id = f"run-{skill_id}-{index}"
        vault.receipt(request_id, {"event": "feedback", "request_id": request_id, "skill": skill_id,
                                   "status": "successful" if useful else "unsuccessful",
                                   **({"skill_hash": skill_hash} if skill_hash else {})})


class FakeStore:
    def __init__(self, tasks):
        self._tasks = tasks

    def list(self, status=None, limit=50, offset=0, origin=None):
        items = [task for task in self._tasks if status is None or task["status"] == status]
        return items[:limit]


class FakeQueue:
    def __init__(self, tasks=None, store=None):
        self.store = store or FakeStore(tasks or [])
        self.retried: list[str] = []

    async def retry(self, task_id):
        self.retried.append(task_id)
        return {"id": task_id, "status": "queued"}


def failed_task(task_id="task-1", prompt="Send the weekly client report", attempts=3):
    return {"id": task_id, "prompt": prompt, "status": "failed", "attempts": attempts,
            "max_attempts": 3, "error": "provider quota exhausted", "retryable": False}


def completed_task(task_id, prompt):
    return {"id": task_id, "prompt": prompt, "status": "completed", "attempts": 1,
            "max_attempts": 3, "error": None}


def write_automations(tmp_path: Path, jobs: list[dict]) -> None:
    blocks = []
    for job in jobs:
        blocks.append('[[jobs]]\n' + "\n".join(f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {str(value).lower()}"
                                               for key, value in job.items()))
    (tmp_path / "vault" / "automations.toml").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def test_normalize_prompt_groups_variants():
    assert normalize_prompt("Send the daily report 2026-10-17") == normalize_prompt("send the daily Report, 2026-10-18")
    assert normalize_prompt("send the daily report") == normalize_prompt("Send the daily report 2")
    assert normalize_prompt("different task") != normalize_prompt("another task")


def test_failed_task_suggestion_never_auto_executes(tmp_path):
    service = make_service(tmp_path)
    queue = FakeQueue([failed_task("task-1")])
    service.queue = queue
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "failed_task_recovery")
    assert item["evidence"][0]["id"] == "task-1"
    assert "quota" in item["why"] and item["decision"] == "active"
    assert queue.retried == []  # evaluation never executes anything
    result = asyncio.run(service.suggestion_act(item["id"], "retry_task"))
    assert result["executed"] is True and queue.retried == ["task-1"]
    with pytest.raises(ValueError, match="not available"):
        asyncio.run(service.suggestion_act(item["id"], "delete_everything"))


def test_dismiss_and_snooze_filter_and_persist(tmp_path):
    service = make_service(tmp_path)
    service.queue = FakeQueue([failed_task("task-1"), failed_task("task-2", prompt="Pay the studio invoice")])
    report = service.suggestions_report()
    ids = [entry["id"] for entry in report["items"]]
    assert len(ids) == 2
    service.suggestions.dismiss(ids[0])
    service.suggestions.snooze(ids[1], seconds=3600)
    report = service.suggestions_report()
    assert report["items"] == [] and report["total"] == 0
    later = service.suggestions.evaluate(service, now=time.time() + 3601, use_cache=False)
    assert [entry["id"] for entry in later["items"]] == [ids[1]]
    assert later["items"][0]["decision"] == "active"
    state = tmp_path / "state" / "suggestions.json"
    assert (state.stat().st_mode & 0o777) == 0o600
    with pytest.raises(ValueError, match="Invalid suggestion id"):
        service.suggestions.dismiss("not-a-suggestion")


def test_repeated_workflow_needs_three_and_skips_skill_prompts(tmp_path):
    service = make_service(tmp_path)
    service.queue = FakeQueue([
        completed_task("t1", "Draft the weekly status update for the team"),
        completed_task("t2", "draft the weekly status update for the team 2026"),
        completed_task("t3", "Draft the weekly status update, for the team!"),
        completed_task("t4", "an unrelated errand request"),
    ])
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "repeated_workflow")
    assert item["why"].startswith("This request completed 3 times")
    assert {entry["id"] for entry in item["evidence"]} == {"t1", "t2", "t3"}
    # Once a skill covers the prompt, the suggestion disappears (no auto-creation).
    write_skill(tmp_path / "skills", "weekly-status", manifest={"trigger_phrases": ["weekly status update"]})
    report = service.suggestions.evaluate(service, use_cache=False)
    assert not [entry for entry in report["items"] if entry["kind"] == "repeated_workflow"]


def test_automation_candidate_flow_and_review_action(tmp_path):
    write_skill(tmp_path / "skills", "demo")
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=skill.content_hash)
    service._receipt_index = None
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "automation_candidate")
    assert item["evidence"][0]["id"] == "demo" and "5" in item["title"]
    assert item["actions"][0]["action"] == "request_review" and item["actions"][0]["available"] is True
    result = asyncio.run(service.suggestion_act(item["id"], "request_review"))
    assert result["executed"] is True and result["result"]["status"] == "pending_review"
    report = service.suggestions_report()
    assert not [entry for entry in report["items"] if entry["kind"] == "automation_candidate"]


def test_declared_tests_block_review_until_passed(tmp_path):
    write_skill(tmp_path / "skills", "demo", manifest={"tests": [
        {"name": "matches", "prompt": "demo thing", "expect": {"skill_id": "demo"}}]})
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=skill.content_hash)
    service._receipt_index = None
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "automation_candidate")
    action = item["actions"][0]
    assert action["available"] is False and "tests" in action["reason"].lower()
    with pytest.raises(ValueError, match="Declared tests"):
        asyncio.run(service.suggestion_act(item["id"], "request_review"))
    service.run_skill_tests("demo")
    result = asyncio.run(service.suggestion_act(item["id"], "request_review"))
    assert result["executed"] is True


def test_scheduled_job_suggestions_for_unapproved_and_missing_skills(tmp_path):
    write_skill(tmp_path / "skills", "demo")
    service = make_service(tmp_path)
    write_automations(tmp_path, [
        {"id": "demo-job", "enabled": True, "skill_id": "demo", "interval_minutes": 60, "prompt": "demo thing"},
        {"id": "ghost-job", "enabled": True, "skill_id": "ghost", "interval_minutes": 60, "prompt": "ghost thing"},
    ])
    report = service.suggestions_report()
    jobs = {entry["evidence"][0]["id"]: entry for entry in report["items"] if entry["kind"] == "scheduled_job"}
    assert "promotion is not_started" in jobs["demo-job"]["why"]
    assert "not in the registry" in jobs["ghost-job"]["why"]


def test_stale_approval_suggestion_and_scoped_recovery(tmp_path):
    write_skill(tmp_path / "skills", "demo")
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=skill.content_hash)
    service._receipt_index = None
    service.request_promotion("demo")
    service.approve_promotion("demo")
    (tmp_path / "skills" / "automation" / "demo" / "SKILL.md").write_text("v2 instructions", encoding="utf-8")
    service.sync_history()
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "automation_stale")
    assert "no longer matches" in item["why"]
    action = item["actions"][0]
    assert action["available"] is False and "Five user-confirmed" in action["reason"]
    with pytest.raises(ValueError, match="Five user-confirmed"):
        asyncio.run(service.suggestion_act(item["id"], "request_review"))
    current = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=current.content_hash)
    service._receipt_index = None
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "automation_stale")
    result = asyncio.run(service.suggestion_act(item["id"], "request_review"))
    assert result["executed"] is True and result["result"]["skill_hash"] == current.content_hash


def test_reliability_review_after_repeated_failures(tmp_path):
    write_skill(tmp_path / "skills", "demo")
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 2, skill_hash=skill.content_hash, useful=False)
    service._receipt_index = None
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "reliability_review")
    assert "2 failed runs" in item["title"]


def test_output_is_bounded_and_survives_missing_queue(tmp_path):
    service = make_service(tmp_path)
    assert service.suggestions_report()["total"] >= 0  # no queue: no crash
    service.queue = FakeQueue([failed_task(f"task-{index}", prompt=f"task number {index} failed") for index in range(12)])
    service.suggestions._cache = None
    report = service.suggestions_report()
    failed = [entry for entry in report["items"] if entry["kind"] == "failed_task_recovery"]
    assert len(failed) == 5 and report["total"] <= 20


def test_real_queue_store_feeds_failed_task_suggestions(tmp_path):
    service = make_service(tmp_path)
    store = QueueStore(tmp_path / "state" / "queue.db")
    service.queue = FakeQueue(store=store)
    task, _ = store.enqueue("Send the overdue invoice reminder")
    store.claim("w1")
    store.fail(task["id"], "agent backend unavailable", retryable=False, backoff_base=1, backoff_cap=2)
    report = service.suggestions_report()
    item = next(entry for entry in report["items"] if entry["kind"] == "failed_task_recovery")
    assert item["evidence"][0]["id"] == task["id"]
    result = asyncio.run(service.suggestion_act(item["id"], "retry_task"))
    assert result["executed"] is True
    store.close()
