"""Drafts: local, inspectable artifacts that never execute anything themselves.

A draft is created from voice or text, can be edited/regenerated, and only
becomes an action through an explicit save/approval step elsewhere (vault note,
durable queue task, or — once wired — a connector operation under the approval
gate). Draft files live under ``state/drafts/`` with 0600 permissions and are
never exposed in logs or receipts beyond id/kind/target metadata.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

KINDS = {"note", "task", "email", "connector"}
DRAFT_ID_PATTERN = re.compile(r"^draft-[0-9a-f]{8}$")
REVISION_LIMIT = 5


class DraftStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, draft_id: str) -> Path:
        return self.root / f"{draft_id}.json"

    def _write(self, draft: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        payload = json.dumps(draft, indent=2, ensure_ascii=False, sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix=".draft-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path(draft["id"]))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _validate_target(kind: str, target) -> dict:
        if target is None:
            return {}
        if not isinstance(target, dict):
            raise ValueError("target must be an object")
        if "to" in target and (not isinstance(target["to"], list)
                               or not all(isinstance(item, str) for item in target["to"])):
            raise ValueError("target.to must be a list of addresses")
        if "subject" in target and not isinstance(target["subject"], str):
            raise ValueError("target.subject must be a string")
        if kind == "connector":
            for key in ("connector", "operation"):
                if key in target and not isinstance(target[key], str):
                    raise ValueError(f"target.{key} must be a string")
            if "args" in target and not isinstance(target["args"], dict):
                raise ValueError("target.args must be an object")
        return target

    def create(self, *, kind: str, body: str, title: str | None = None, target: dict | None = None,
               transcript: str | None = None, source: str = "text") -> dict:
        kind = (kind or "note").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"Unknown draft kind: {kind!r}; expected note, task, email, or connector")
        body = (body or "").strip()
        if not body:
            raise ValueError("A draft needs non-empty content")
        if source not in {"voice", "text"}:
            source = "text"
        target = self._validate_target(kind, dict(target) if target else None)
        now = time.time()
        draft = {"id": f"draft-{secrets.token_hex(4)}", "kind": kind,
                 "title": (title or "").strip() or body.splitlines()[0][:80],
                 "body": body, "target": target, "source": source,
                 "transcript": transcript, "status": "draft",
                 "saved_ref": None, "created_at": now, "updated_at": now,
                 "revision": 1, "revisions": []}
        self._write(draft)
        return draft

    def get(self, draft_id: str) -> dict | None:
        if not isinstance(draft_id, str) or not DRAFT_ID_PATTERN.match(draft_id):
            return None
        try:
            data = json.loads(self._path(draft_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        drafts = []
        for path in sorted(self.root.glob("draft-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and DRAFT_ID_PATTERN.match(str(data.get("id", ""))):
                drafts.append(data)
        return sorted(drafts, key=lambda item: item.get("updated_at", 0), reverse=True)

    def update(self, draft_id: str, *, title: str | None = None, body: str | None = None,
               target: dict | None = None, source: str = "edit") -> dict:
        draft = self.get(draft_id)
        if not draft:
            raise ValueError("Draft not found")
        if title is None and body is None and target is None:
            raise ValueError("Nothing to update")
        cleaned_body = None
        if body is not None:
            cleaned_body = body.strip()
            if not cleaned_body:
                raise ValueError("A draft needs non-empty content")
        revisions = draft.setdefault("revisions", [])
        revisions.append({"at": draft.get("updated_at"), "title": draft.get("title"),
                          "body": draft.get("body"), "source": source})
        draft["revisions"] = revisions[-REVISION_LIMIT:]
        if draft.get("status") == "saved":
            # The saved artifact no longer matches once the draft changes.
            draft["status"] = "draft"
            draft["saved_ref"] = None
        if title is not None:
            draft["title"] = title.strip() or draft["title"]
        if cleaned_body is not None:
            draft["body"] = cleaned_body
        if target is not None:
            draft["target"] = self._validate_target(draft["kind"], dict(target))
        draft["revision"] = int(draft.get("revision", 1)) + 1
        draft["updated_at"] = time.time()
        self._write(draft)
        return draft

    def mark_saved(self, draft_id: str, reference: str) -> dict:
        draft = self.get(draft_id)
        if not draft:
            raise ValueError("Draft not found")
        draft["status"] = "saved"
        draft["saved_ref"] = reference
        draft["updated_at"] = time.time()
        self._write(draft)
        return draft

    def delete(self, draft_id: str) -> bool:
        if not isinstance(draft_id, str) or not DRAFT_ID_PATTERN.match(draft_id):
            return False
        path = self._path(draft_id)
        if not path.exists():
            return False
        path.unlink()
        return True
