"""Stage 7 drafts tests: store roundtrip, revision history, privacy, and save
boundaries (notes -> vault, tasks -> queue or the existing submit gate,
connector kinds explicitly blocked until the connector framework lands)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cortexos.config import Settings
from cortexos.service import CortexService
from cortexos.vault import Vault


def make_service(tmp_path: Path) -> CortexService:
    (tmp_path / "skills").mkdir(exist_ok=True)
    (tmp_path / "vault").mkdir(exist_ok=True)
    settings = Settings(root=tmp_path, vault=tmp_path / "vault", skills=tmp_path / "skills",
                        queue_db=tmp_path / "state" / "queue.db")
    return CortexService(settings)


class FakeQueue:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def enqueue(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return {"task": {"id": "task-123"}, "created": True}


def test_store_roundtrip_update_list_discard(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="note", body="Call the accountant about Q3.", title="Accountant")
    assert draft["id"].startswith("draft-") and draft["status"] == "draft" and draft["revision"] == 1
    drafts_dir = tmp_path / "state" / "drafts"
    assert drafts_dir.stat().st_mode & 0o777 == 0o700
    assert (drafts_dir / f"{draft['id']}.json").stat().st_mode & 0o777 == 0o600
    fetched = service.drafts.get(draft["id"])
    assert fetched["title"] == "Accountant"
    assert [item["id"] for item in service.drafts.list()] == [draft["id"]]
    updated = service.drafts.update(draft["id"], body="Call the accountant about Q3 and Q4.")
    assert updated["revision"] == 2 and updated["revisions"][0]["body"] == "Call the accountant about Q3."
    assert service.drafts.delete(draft["id"]) is True
    assert service.drafts.get(draft["id"]) is None
    assert service.drafts.delete(draft["id"]) is False
    # Invalid ids never touch the filesystem
    assert service.drafts.get("../../etc/passwd") is None
    assert service.drafts.delete("draft-zzzzzzzz") is False


def test_draft_validation_rejects_bad_input(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        service.create_draft(kind="note", body="   ")
    with pytest.raises(ValueError, match="Unknown draft kind"):
        service.create_draft(kind="magic", body="hello")
    with pytest.raises(ValueError, match="target.to"):
        service.create_draft(kind="email", body="hello", target={"to": "not-a-list"})
    with pytest.raises(ValueError, match="target.args"):
        service.create_draft(kind="connector", body="hello", target={"connector": "gmail", "args": "nope"})
    with pytest.raises(ValueError, match="Nothing to update"):
        service.drafts.update(service.create_draft(kind="note", body="x")["id"])


def test_voice_draft_preserves_transcript_and_privacy(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="email", body="remind Bob about the invoice",
                                 transcript="remind bob about the uh invoice", source="voice",
                                 target={"to": ["bob@example.com"], "subject": "Invoice"})
    assert draft["source"] == "voice"
    assert draft["transcript"] == "remind bob about the uh invoice"
    assert draft["target"]["to"] == ["bob@example.com"]
    assert service.draft_execution(draft)["available"] is False  # connector execution not wired yet
    path = tmp_path / "state" / "drafts" / f"{draft['id']}.json"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_revision_history_is_capped(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="note", body="v1")
    for index in range(2, 9):
        draft = service.drafts.update(draft["id"], body=f"v{index}")
    assert draft["revision"] == 8
    assert len(draft["revisions"]) == 5
    assert draft["body"] == "v8"


def test_save_note_writes_vault_output_without_duplicating_content(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="note", body="Buy oat milk.", title="Groceries")
    result = asyncio.run(service.save_draft(draft["id"]))
    assert result["status"] == "saved"
    reference = Path(result["reference"])
    assert reference.exists() and reference.suffix == ".md"
    assert "Buy oat milk." in reference.read_text(encoding="utf-8")
    saved = service.drafts.get(draft["id"])
    assert saved["status"] == "saved" and saved["saved_ref"] == str(reference)
    again = asyncio.run(service.save_draft(draft["id"]))
    assert again["status"] == "already_saved"
    receipts = [Vault._receipt_data(path) for path in (tmp_path / "vault" / "receipts").glob("**/*.md")]
    payload = next(item for item in receipts if item.get("event") == "draft_saved")
    assert payload["draft_id"] == draft["id"] and payload["kind"] == "note"
    assert "body" not in payload  # receipts never duplicate draft content
    # Editing a saved draft re-opens it: the saved reference no longer matches.
    edited = service.drafts.update(draft["id"], body="Buy oat milk and bread.")
    assert edited["status"] == "draft" and edited["saved_ref"] is None


def test_save_task_uses_queue_and_approval_gate_stays_downstream(tmp_path):
    service = make_service(tmp_path)
    queue = FakeQueue()
    service.queue = queue
    draft = service.create_draft(kind="task", body="Prepare the weekly summary", source="voice",
                                 transcript="prepare the weekly summary")
    result = asyncio.run(service.save_draft(draft["id"]))
    assert result["status"] == "saved" and result["reference"] == "task-123"
    prompt, kwargs = queue.calls[0]
    assert prompt == "Prepare the weekly summary"
    assert kwargs["origin"] == f"draft:{draft['id']}"
    assert kwargs["meta"]["draft_id"] == draft["id"]
    assert service.drafts.get(draft["id"])["saved_ref"] == "task-123"
    receipt = [Vault._receipt_data(path) for path in (tmp_path / "vault" / "receipts").glob("**/*.md")]
    saved = next(item for item in receipt if item.get("event") == "draft_saved")
    assert saved["kind"] == "task" and saved["target"] == "task-123"


def test_save_task_without_queue_submits_directly(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="task", body="list skills")
    result = asyncio.run(service.save_draft(draft["id"]))
    assert result["status"] == "saved" and result["state"] == "completed"
    assert service.drafts.get(draft["id"])["saved_ref"] == f"{draft['id']}-run"


def test_connector_and_email_save_is_blocked_honestly(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="connector", body="upload the report",
                                 target={"connector": "drive", "operation": "upload"})
    with pytest.raises(ValueError, match="not wired"):
        asyncio.run(service.save_draft(draft["id"]))
    assert service.drafts.get(draft["id"])["status"] == "draft"


def test_regenerate_without_fast_model_fails_honestly(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="note", body="messy text", transcript="messy text")
    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(service.regenerate_draft(draft["id"]))
    assert service.drafts.get(draft["id"])["body"] == "messy text"


def test_regenerate_uses_assist_and_keeps_history(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="note", body="messy text", transcript="messy text")

    async def fake_assist(prompt, **kwargs):
        assert "messy text" in prompt
        return "Clean text."

    service.fast_model.assist = fake_assist
    result = asyncio.run(service.regenerate_draft(draft["id"]))
    assert result["body"] == "Clean text."
    assert result["revision"] == 2
    assert result["revisions"][-1]["source"] == "regenerate"
    assert result["revisions"][-1]["body"] == "messy text"


def test_saved_draft_regenerate_is_refused(tmp_path):
    service = make_service(tmp_path)
    draft = service.create_draft(kind="note", body="keep me")
    asyncio.run(service.save_draft(draft["id"]))
    with pytest.raises(ValueError, match="Saved drafts"):
        asyncio.run(service.regenerate_draft(draft["id"]))
