from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def now() -> datetime:
    return datetime.now(ZoneInfo("Africa/Kampala"))


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")[:60] or "request"


class Vault:
    def __init__(self, root: Path):
        self.root = root

    def bootstrap(self) -> None:
        for folder in ("raw", "wiki", "output", "requests", "receipts", "metrics"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        index = self.root / "wiki" / "_master-index.md"
        if not index.exists():
            index.write_text("# CortexOS Wiki Index\n\nAdd durable reference notes here. Keep this index as the fast entry point for the vault.\n", encoding="utf-8")
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text("# CortexOS Vault\n\n`raw/` is user-authored capture; `wiki/` is organized knowledge; `output/` contains generated deliverables; `requests/` and `receipts/` contain append-only logs; `metrics/` contains user-provided metrics.\n", encoding="utf-8")
        metric_templates = {
            "audience.md": "# Audience metrics\n\nFill these fields with real, dated figures. Leave unknown values blank.\n\n- Updated: \n- Platform: \n- Followers/subscribers: \n- Period change: \n- Source: \n",
            "usage.md": "# CortexOS usage metrics\n\nFill these fields with actual local usage. Leave unknown values blank.\n\n- Updated: \n- Requests this period: \n- Skill runs this period: \n- Source / calculation: \n",
        }
        for name, content in metric_templates.items():
            path = self.root / "metrics" / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        automations = self.root / "automations.toml"
        if not automations.exists():
            automations.write_text("# Scheduled jobs run only after the skill has been approved for automation.\n# Add a [[jobs]] block with enabled = true, skill_id, interval_minutes, and prompt.\n# Per-run send/spend/publish approval gates still apply.\n", encoding="utf-8")

    def _dated_path(self, area: str, key: str, extension: str = "md") -> Path:
        date = now()
        folder = self.root / area / date.strftime("%Y") / date.strftime("%m")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{date.strftime('%Y%m%d-%H%M%S')}-{safe_name(key)}.{extension}"

    def log_request(self, request_id: str, prompt: str, status: str, tier: int | None = None, backend: str | None = None, output: str | None = None) -> Path:
        self.bootstrap()
        request_folder = self.root / "requests"
        request_folder.mkdir(parents=True, exist_ok=True)
        path = request_folder / f"{safe_name(request_id)}.md"
        if path.exists():
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n## Update — {now().isoformat()}\n\n- Status: `{status}`\n- Tier: `{tier or ''}`\n- Backend: `{backend or ''}`\n")
                if output:
                    stream.write(f"- Output: `{output}`\n")
            return path
        path.write_text(f"---\nid: {request_id}\ncreated: {now().isoformat()}\nstatus: {status}\ntier: {tier or ''}\nbackend: {backend or ''}\n---\n\n# Request\n\n{prompt}\n", encoding="utf-8")
        return path

    def write_result(self, request_id: str, prompt: str, result: str, skill_id: str | None = None) -> Path:
        self.bootstrap()
        title = (skill_id or "answer").replace("-", " ").title()
        path = self._dated_path("output", request_id)
        path.write_text(f"---\nid: {request_id}\ncreated: {now().isoformat()}\nskill: {skill_id or ''}\n---\n\n# {title}\n\n## Request\n\n{prompt}\n\n## Result\n\n{result}\n", encoding="utf-8")
        return path

    def receipt(self, request_id: str, payload: dict) -> Path:
        self.bootstrap()
        path = self._dated_path("receipts", request_id)
        path.write_text(f"---\nid: {request_id}\ncreated: {now().isoformat()}\n---\n\n# Skill receipt\n\n```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```\n", encoding="utf-8")
        return path

    def skill_feedback(self, request_id: str, skill_id: str, useful: bool) -> Path:
        # A success vote must refer to a completed run of this exact skill.
        if not any(self._receipt_data(path).get("status") == "completed" and
                   self._receipt_data(path).get("skill") == skill_id
                   for path in (self.root / "receipts").glob("**/*.md")
                   if self._receipt_data(path).get("event") != "feedback" and
                   self._receipt_data(path).get("request_id") == request_id):
            raise ValueError("No completed receipt for this skill and request ID")
        if any(self._receipt_data(path).get("event") == "feedback" and
               self._receipt_data(path).get("request_id") == request_id
               for path in (self.root / "receipts").glob("**/*.md")):
            raise ValueError("Feedback has already been recorded for this run")
        return self.receipt(f"{request_id}-feedback", {"event": "feedback", "request_id": request_id,
                                                         "skill": skill_id, "status": "successful" if useful else "unsuccessful"})

    @staticmethod
    def _receipt_data(path: Path) -> dict:
        text = path.read_text(encoding="utf-8")
        match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(1))
            header_id = re.search(r"(?m)^id:\s*(.+)$", text)
            data.setdefault("request_id", header_id.group(1).strip() if header_id else path.stem)
            return data
        except ValueError:
            return {}

    def promotion(self, skill_id: str) -> dict:
        path = self.root / "receipts" / "promotions" / f"{safe_name(skill_id)}.md"
        if not path.exists():
            return {"status": "not_started", "history": []}
        data = self._receipt_data(path)
        return data if data.get("skill") == skill_id else {"status": "not_started", "history": []}

    def promote_skill(self, skill_id: str) -> dict:
        stats = self.skill_stats(skill_id)
        current = self.promotion(skill_id)
        if stats["successful_runs"] < 5:
            raise ValueError("Five user-confirmed successful runs are required")
        if current["status"] in {"pending_review", "approved"}:
            return current
        history = current.get("history", [])
        history.append({"status": "pending_review", "at": now().isoformat()})
        data = {"event": "promotion", "skill": skill_id, "status": "pending_review", "history": history}
        self._write_promotion(skill_id, data)
        return data

    def approve_promotion(self, skill_id: str) -> dict:
        current = self.promotion(skill_id)
        if current["status"] != "pending_review":
            raise ValueError("Skill must be pending automation review")
        history = current.get("history", [])
        history.append({"status": "approved", "at": now().isoformat()})
        data = {"event": "promotion", "skill": skill_id, "status": "approved", "history": history}
        self._write_promotion(skill_id, data)
        return data

    def _write_promotion(self, skill_id: str, data: dict) -> None:
        path = self.root / "receipts" / "promotions" / f"{safe_name(skill_id)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Automation promotion ledger\n\n```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```\n", encoding="utf-8")

    def metrics(self) -> list[dict[str, str]]:
        folder = self.root / "metrics"
        if not folder.exists():
            return []
        entries = []
        for path in sorted(p for p in folder.glob("**/*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".csv"}):
            if path.name.lower() == "readme.md":
                continue
            entries.append({"name": path.stem.replace("-", " ").replace("_", " ").title(),
                            "path": str(path.relative_to(self.root)), "content": path.read_text(encoding="utf-8")[:500]})
        return entries

    def skill_stats(self, skill_id: str) -> dict:
        feedback_events: dict[str, str] = {}
        for path in sorted((self.root / "receipts").glob("**/*.md")):
            text = path.read_text(encoding="utf-8")
            match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
            if not match:
                continue
            try:
                data = json.loads(match.group(1))
            except ValueError:
                continue
            if data.get("skill") != skill_id:
                continue
            if data.get("event") != "feedback":
                continue
            feedback_events[data.get("request_id", path.stem)] = data.get("status", "")
        statuses = list(feedback_events.values())
        successes = statuses.count("successful")
        errors = statuses.count("unsuccessful")
        promotion = self.promotion(skill_id)
        return {"successful_runs": successes, "error_runs": errors, "automation_review_due": successes >= 5,
                "promotion_status": promotion["status"]}
