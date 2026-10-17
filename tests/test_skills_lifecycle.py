"""Stage 7 skills lifecycle tests: validation, versioning/history, rollback,
hash-scoped evidence, promotion policy, deterministic test receipts, and the
scheduler version gate."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cortexos.config import Settings
from cortexos.scheduler import scheduler_tick
from cortexos.service import CortexService
from cortexos.skills import HISTORY_LIMIT, SkillRegistry
from cortexos.vault import Vault


def write_skill(root: Path, skill_id: str = "demo", *, instructions: str = "Demo instructions v1.",
                manifest: dict | None = None, folder: str | None = None) -> Path:
    base = root / "automation" / (folder or skill_id)
    base.mkdir(parents=True, exist_ok=True)
    data = {"id": skill_id, "name": "Demo Skill", "domain": "Automation", "description": "demo skill",
            "trigger_phrases": ["demo thing"], "tier": 3, "risk_level": "read_only",
            "approval_required": False, "automation_ready": False, "instructions": "SKILL.md"}
    if manifest:
        data.update(manifest)
    (base / "skill.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (base / "SKILL.md").write_text(instructions, encoding="utf-8")
    return base


def make_service(tmp_path: Path) -> CortexService:
    (tmp_path / "skills").mkdir(exist_ok=True)
    (tmp_path / "vault").mkdir(exist_ok=True)
    settings = Settings(root=tmp_path, vault=tmp_path / "vault", skills=tmp_path / "skills",
                        queue_db=tmp_path / "state" / "queue.db")
    return CortexService(settings)


def feedback_receipts(vault: Vault, skill_id: str, count: int, *, skill_hash: str | None,
                      useful: bool = True, prefix: str = "run") -> None:
    for index in range(count):
        request_id = f"{prefix}-{skill_id}-{index}"
        vault.receipt(request_id, {"event": "feedback", "request_id": request_id, "skill": skill_id,
                                   "status": "successful" if useful else "unsuccessful",
                                   **({"skill_hash": skill_hash} if skill_hash else {})})


def bust(service: CortexService) -> None:
    service._receipt_index = None


def promotion_path(tmp_path: Path, skill_id: str) -> Path:
    return tmp_path / "vault" / "receipts" / "promotions" / f"{skill_id}.md"


def write_automation(tmp_path: Path, skill_id: str = "demo", job_id: str = "demo-job") -> None:
    (tmp_path / "vault" / "automations.toml").write_text(
        f'[[jobs]]\nid = "{job_id}"\nenabled = true\nskill_id = "{skill_id}"\n'
        f'interval_minutes = 60\nprompt = "demo thing"\n', encoding="utf-8")


# ------------------------------------------------------------------ validation


def test_manifest_validation_loads_and_derives_version(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    registry = SkillRegistry(root)
    skill = registry.get("demo")
    assert skill is not None
    assert skill.version == "1.0.0" and skill.version_source == "derived"
    assert skill.problems == []
    assert skill.content_hash.startswith("sha256:")
    assert registry.scan().problems == []


def test_manifest_rejections_and_conservative_defaults(tmp_path):
    root = tmp_path / "skills"
    broken = root / "automation" / "broken"
    broken.mkdir(parents=True)
    (broken / "skill.json").write_text("{not json", encoding="utf-8")
    write_skill(root, "escape", manifest={"instructions": "../outside.md"})
    write_skill(root, "noid", manifest={"id": None})
    registry = SkillRegistry(root)
    scan = registry.scan()
    ids = {item.id for item in scan.skills}
    assert "escape" not in ids and "noid" not in ids and "broken" not in ids
    assert len([item for item in scan.problems if item["rejected"]]) == 3

    write_skill(root, "loose", manifest={"tier": 9, "risk_level": "whatever", "approval_required": "yes"})
    loose = registry.get("loose")
    assert loose.tier == 3 and loose.risk_level == "unknown" and loose.approval_required is True
    assert loose.problems

    write_skill(root, "other-dir", manifest={"id": "mismatch"})
    assert registry.get("mismatch") is not None
    mismatch = [item for item in registry.scan().problems if item["id"] == "mismatch"]
    assert mismatch and mismatch[0]["rejected"] is False


def test_content_hash_tracks_semantics_not_formatting(tmp_path):
    root = tmp_path / "skills"
    base = write_skill(root, "demo")
    first = SkillRegistry(root).get("demo").content_hash
    data = json.loads((base / "skill.json").read_text(encoding="utf-8"))
    (base / "skill.json").write_text(json.dumps(data, indent=4, sort_keys=True), encoding="utf-8")
    assert SkillRegistry(root).get("demo").content_hash == first
    (base / "SKILL.md").write_text("Demo instructions v2.", encoding="utf-8")
    after_instructions = SkillRegistry(root).get("demo").content_hash
    assert after_instructions != first
    data["description"] = "a different description"
    (base / "skill.json").write_text(json.dumps(data), encoding="utf-8")
    assert SkillRegistry(root).get("demo").content_hash != after_instructions


# --------------------------------------------------------------------- history


def test_history_baseline_then_change_entries(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    entries = service.history.entries("demo")
    assert len(entries) == 1 and entries[0]["source"] == "baseline"
    assert service.sync_history()["recorded"] == []
    (root / "automation" / "demo" / "SKILL.md").write_text("Demo instructions v2.", encoding="utf-8")
    recorded = service.sync_history()["recorded"]
    assert [item["source"] for item in recorded] == ["change"]
    view = service.history_view("demo")
    assert view["entries"][0]["current"] is True
    assert [item["source"] for item in view["entries"]] == ["change", "baseline"]
    assert view["baseline_hash"] == view["entries"][1]["hash"]


def test_history_is_capped(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    for index in range(HISTORY_LIMIT + 5):
        (root / "automation" / "demo" / "SKILL.md").write_text(f"instructions rev {index}", encoding="utf-8")
        service.sync_history()
    assert len(service.history.entries("demo")) == HISTORY_LIMIT


def test_rollback_requires_confirmation_and_known_version(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    baseline = service.history.entries("demo")[0]["hash"]
    with pytest.raises(ValueError, match="confirmation"):
        service.rollback_skill("demo", baseline, confirm=False)
    with pytest.raises(ValueError, match="Unknown version"):
        service.rollback_skill("demo", "sha256:00000000", confirm=True)
    with pytest.raises(ValueError, match="already current"):
        service.rollback_skill("demo", baseline, confirm=True)


def test_rollback_restores_version_and_records_evidence(tmp_path):
    root = tmp_path / "skills"
    skill_dir = write_skill(root, "demo", instructions="Demo instructions v1.")
    service = make_service(tmp_path)
    baseline = service.history.entries("demo")[0]["hash"]
    (skill_dir / "SKILL.md").write_text("Demo instructions v2.", encoding="utf-8")
    service.sync_history()
    changed_hash = service.registry.get("demo").content_hash
    assert changed_hash != baseline
    result = service.rollback_skill("demo", baseline, confirm=True)
    assert result["status"] == "rolled_back" and result["to_version"] == "1.0.0"
    restored = service.registry.get("demo")
    assert restored.content_hash == baseline
    assert "v1" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    entries = service.history.entries("demo")
    assert entries[-1]["source"] == "rollback" and entries[-1]["hash"] == baseline
    assert service.history.entry("demo", changed_hash) is not None
    view = service.history_entry("demo", baseline)
    assert "v1" in view["instructions"]
    receipts = [Vault._receipt_data(path) for path in (tmp_path / "vault" / "receipts").glob("**/*.md")]
    assert any(item.get("event") == "skill_rollback" and item.get("status") == "completed" for item in receipts)


# -------------------------------------------------------- scoped evidence/stats


def test_stats_scope_feedback_to_current_hash(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 3, skill_hash=skill.content_hash)
    feedback_receipts(service.vault, "demo", 2, skill_hash="sha256:stale", prefix="old")
    bust(service)
    stats = service.skill_stats("demo")
    assert stats["successful_runs"] == 3
    assert stats["evidence"]["stale_runs"] == 2
    assert stats["evidence"]["hash"] == skill.content_hash


def test_legacy_feedback_counts_only_while_unchanged(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    feedback_receipts(service.vault, "demo", 5, skill_hash=None)
    bust(service)
    stats = service.skill_stats("demo")
    assert stats["successful_runs"] == 5
    assert stats["evidence"]["legacy_runs_counted"] is True
    assert service.vault.skill_stats("demo")["successful_runs"] == 5  # unscoped callers still work
    (root / "automation" / "demo" / "SKILL.md").write_text("changed!", encoding="utf-8")
    service.sync_history()
    bust(service)
    stats = service.skill_stats("demo")
    assert stats["successful_runs"] == 0 and stats["error_runs"] == 0
    assert stats["evidence"]["stale_runs"] == 5


def test_error_feedback_counts_against_skill(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    feedback_receipts(service.vault, "demo", 2, skill_hash=None, useful=False)
    bust(service)
    stats = service.skill_stats("demo")
    assert stats["error_runs"] == 2 and stats["successful_runs"] == 0


# ------------------------------------------------------------------- promotion


def test_promotion_requires_five_scoped_runs_and_records_hash(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 4, skill_hash=skill.content_hash)
    bust(service)
    with pytest.raises(ValueError, match="Five user-confirmed"):
        service.request_promotion("demo")
    feedback_receipts(service.vault, "demo", 1, skill_hash=skill.content_hash, prefix="fifth")
    bust(service)
    ledger = service.request_promotion("demo")
    assert ledger["status"] == "pending_review" and ledger["skill_hash"] == skill.content_hash
    assert ledger["evidence"]["successful_runs"] == 5
    approved = service.approve_promotion("demo")
    assert approved["status"] == "approved" and approved["skill_hash"] == skill.content_hash
    assert service.automation_permitted(service.registry.get("demo"))[0] is True
    assert service.skill_stats("demo")["automation_permitted"] is True


def test_change_invalidates_approval_and_opens_new_review(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=skill.content_hash)
    bust(service)
    service.request_promotion("demo")
    service.approve_promotion("demo")
    (root / "automation" / "demo" / "SKILL.md").write_text("Demo instructions v2.", encoding="utf-8")
    service.sync_history()
    current = service.registry.get("demo")
    permitted, reason = service.automation_permitted(current)
    assert permitted is False and "changed" in reason
    feedback_receipts(service.vault, "demo", 5, skill_hash=current.content_hash, prefix="v2")
    bust(service)
    ledger = service.request_promotion("demo")
    assert ledger["status"] == "pending_review" and ledger["skill_hash"] == current.content_hash


# ------------------------------------------------------------------ skill tests


def test_declared_tests_gate_promotion_and_scope_to_hash(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo", manifest={"tier": 1, "tests": [
        {"name": "matches trigger", "prompt": "please demo thing now", "expect": {"skill_id": "demo", "tier": 1}},
        {"name": "safe gate", "prompt": "please demo thing now", "expect": {"requires_approval": False}}]})
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=skill.content_hash)
    bust(service)
    with pytest.raises(ValueError, match="Declared tests"):
        service.request_promotion("demo")
    result = service.run_skill_tests("demo")
    assert result["status"] == "passed" and result["passed"] == 2 and result["total"] == 2
    assert result["cases"][0]["actual"]["matched_skill"] == "demo"
    assert service.skill_stats("demo")["evidence"]["test"]["status"] == "passed"
    ledger = service.request_promotion("demo")
    assert ledger["status"] == "pending_review"
    # A change invalidates the test evidence: review is blocked again until re-tested.
    (root / "automation" / "demo" / "SKILL.md").write_text("Demo instructions v2.", encoding="utf-8")
    service.sync_history()
    (tmp_path / "vault" / "receipts" / "promotions" / "demo.md").unlink()
    current = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=current.content_hash, prefix="v2")
    bust(service)
    with pytest.raises(ValueError, match="Declared tests"):
        service.request_promotion("demo")
    service.run_skill_tests("demo")
    assert service.request_promotion("demo")["skill_hash"] == current.content_hash


def test_run_skill_tests_reports_mismatches(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo", manifest={"tier": 3, "tests": [
        {"name": "wrong tier", "prompt": "demo thing", "expect": {"tier": 1}}]})
    service = make_service(tmp_path)
    result = service.run_skill_tests("demo")
    assert result["status"] == "failed" and result["passed"] == 0
    assert result["cases"][0]["mismatches"]
    assert service.skill_stats("demo")["evidence"]["test"]["status"] == "failed"
    receipts = [Vault._receipt_data(path) for path in (tmp_path / "vault" / "receipts").glob("**/*.md")]
    assert any(item.get("event") == "skill_test" and item.get("status") == "failed" for item in receipts)


def test_run_skill_tests_without_declared_tests(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="No tests"):
        service.run_skill_tests("demo")


# ------------------------------------------------------------- receipts/scoping


def test_execution_receipt_enrichment_and_task_id(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo", manifest={"tier": 1})
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    result = asyncio.run(service.submit("demo thing please", request_id="enrich-1", task_id="task-77"))
    assert result["status"] == "completed"
    receipts = [Vault._receipt_data(path) for path in (tmp_path / "vault" / "receipts").glob("**/*.md")]
    data = next(item for item in receipts
                if item.get("request_id") == "enrich-1" and item.get("event") == "execution")
    assert data["task_id"] == "task-77"
    assert data["skill"] == "demo" and data["skill_hash"] == skill.content_hash
    assert data["skill_version"] == "1.0.0"
    assert data["started_at"] and data["finished_at"] and data["duration_ms"] >= 0
    stats = service.skill_stats("demo")
    assert stats["recent_runs"] and stats["recent_runs"][0]["task_id"] == "task-77"


def test_feedback_records_hash_and_counts_scoped(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo", manifest={"tier": 1})
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    asyncio.run(service.submit("demo thing please", request_id="fb-1"))
    saved = service.feedback("fb-1", "demo", True)
    assert saved["status"] == "saved" and saved["skill_hash"] == skill.content_hash
    duplicate = service.feedback("fb-1", "demo", True)
    assert duplicate["status"] == "error" and "already" in duplicate["error"]
    assert service.feedback("fb-1", "ghost", True)["status"] == "error"
    bust(service)
    assert service.skill_stats("demo")["successful_runs"] == 1


# -------------------------------------------------------------------- scheduler


def test_scheduler_skips_jobs_with_stale_approval(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo", manifest={"tier": 1})
    service = make_service(tmp_path)
    skill = service.registry.get("demo")
    feedback_receipts(service.vault, "demo", 5, skill_hash=skill.content_hash)
    bust(service)
    service.request_promotion("demo")
    service.approve_promotion("demo")
    write_automation(tmp_path)
    due = asyncio.run(scheduler_tick(service, dry_run=True, now_epoch=1_800_000_000))
    assert [job["id"] for job in due] == ["demo-job"]
    (root / "automation" / "demo" / "SKILL.md").write_text("Demo instructions v2.", encoding="utf-8")
    service.sync_history()
    due = asyncio.run(scheduler_tick(service, dry_run=True, now_epoch=1_800_000_000))
    assert due == []


def test_scheduler_allows_legacy_approval_until_skill_changes(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "demo")
    service = make_service(tmp_path)
    ledger = promotion_path(tmp_path, "demo")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("# Automation promotion ledger\n\n```json\n" + json.dumps(
        {"event": "promotion", "skill": "demo", "status": "approved", "history": []}) + "\n```\n", encoding="utf-8")
    write_automation(tmp_path)
    due = asyncio.run(scheduler_tick(service, dry_run=True, now_epoch=1_800_000_000))
    assert [job["id"] for job in due] == ["demo-job"]
    (root / "automation" / "demo" / "SKILL.md").write_text("Demo instructions v2.", encoding="utf-8")
    service.sync_history()
    due = asyncio.run(scheduler_tick(service, dry_run=True, now_epoch=1_800_000_000))
    assert due == []
