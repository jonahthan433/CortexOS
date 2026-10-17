from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
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
        agents = self.root / "AGENTS.md"
        if not agents.exists():
            agents.write_text(
                "# CortexOS vault instructions\n\n"
                "- `raw/` is user-authored input; preserve it unless the user asks for edits.\n"
                "- `wiki/` is organized reference material; keep `_master-index.md` current.\n"
                "- `output/` contains generated deliverables.\n"
                "- `requests/` contains request records; `receipts/` contains run, feedback, and promotion history.\n"
                "- `metrics/` contains only real user-provided or measured data; never invent values.\n"
                "- CortexOS skill definitions live in the configured CortexOS repository under `skills/`; Bridge skill runs receive their instructions automatically.\n"
                "- Codex should use this vault as its working directory and follow this file; Claude Code should also follow `CLAUDE.md`.\n"
                "- `automations.toml` contains explicit schedules; only approved skills may be scheduled.\n"
                "- Sending, spending, publishing, or other external side effects require fresh human approval for each run, even after promotion.\n",
                encoding="utf-8",
            )
        claude = self.root / "CLAUDE.md"
        if not claude.exists():
            claude.write_text("# CortexOS vault instructions for Claude Code\n\nFollow [AGENTS.md](AGENTS.md) and preserve its vault paths, user data, and approval rules.\n", encoding="utf-8")
        metric_templates = {
            "audience.toml": '# CortexOS metrics schema v1. Edit values with real data; blank means unknown.\nschema_version = 1\nkind = "audience"\n\n[[metrics]]\nkey = "followers"\nvalue = ""\nunit = "people"\nupdated = ""\nsource = ""\n',
            "usage.toml": '# CortexOS metrics schema v1. Edit values with real data; blank means unknown.\nschema_version = 1\nkind = "usage"\n\n[[metrics]]\nkey = "requests"\nvalue = ""\nunit = "count"\nupdated = ""\nsource = ""\n',
        }
        for name, content in metric_templates.items():
            path = self.root / "metrics" / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        state = self.root / "state.json"
        if not state.exists():
            self.write_state({"version": 1, "pending_approvals": {}, "scheduler_last_run": {}})
        automations = self.root / "automations.toml"
        if not automations.exists():
            automations.write_text("# Scheduled jobs run only after the skill has been approved for automation.\n# Add a [[jobs]] block with enabled = true, skill_id, interval_minutes, and prompt.\n# Per-run send/spend/publish approval gates still apply.\n", encoding="utf-8")

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    PENDING_TTL_SECONDS = 7 * 24 * 3600  # ponytail: flat 7-day TTL, per-item expiry if needed

    def load_state(self) -> dict:
        self.bootstrap()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if value.get("version") == 1:
                value.setdefault("pending_approvals", {})
                value.setdefault("scheduler_last_run", {})
                self._expire_pending(value)
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return {"version": 1, "pending_approvals": {}, "scheduler_last_run": {}}

    def _expire_pending(self, state: dict) -> None:
        """Drop pending approvals older than the TTL; they can never be approved again."""
        cutoff = datetime.now(timezone.utc).timestamp() - self.PENDING_TTL_SECONDS
        stale = [key for key, item in state["pending_approvals"].items()
                 if float(item.get("created_at", 0) or 0) < cutoff]
        for key in stale:
            state["pending_approvals"].pop(key, None)
        if stale:
            self.write_state(state)

    def write_state(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save_pending(self, request_id: str, pending: dict) -> None:
        state = self.load_state()
        state["pending_approvals"][request_id] = pending
        self.write_state(state)

    def remove_pending(self, request_id: str) -> None:
        state = self.load_state()
        state["pending_approvals"].pop(request_id, None)
        self.write_state(state)

    def _dated_path(self, area: str, key: str, extension: str = "md") -> Path:
        date = now()
        folder = self.root / area / date.strftime("%Y") / date.strftime("%m")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{date.strftime('%Y%m%d-%H%M%S')}-{safe_name(key)}.{extension}"

    def request_exists(self, request_id: str) -> bool:
        return (self.root / "requests" / f"{safe_name(request_id)}.md").exists()

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

    def write_note(self, note_id: str, title: str, body: str) -> Path:
        self.bootstrap()
        path = self._dated_path("output", note_id)
        path.write_text(f"---\nid: {note_id}\ncreated: {now().isoformat()}\ntype: note\n---\n\n# {title}\n\n{body}\n", encoding="utf-8")
        return path

    def receipt(self, request_id: str, payload: dict) -> Path:
        self.bootstrap()
        path = self._dated_path("receipts", request_id)
        path.write_text(f"---\nid: {request_id}\ncreated: {now().isoformat()}\n---\n\n# Skill receipt\n\n```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```\n", encoding="utf-8")
        return path

    def skill_feedback(self, request_id: str, skill_id: str, useful: bool,
                       skill_hash: str | None = None) -> Path:
        # One pass over receipts; each file parsed once.
        receipts = [(path, data) for path in (self.root / "receipts").glob("**/*.md")
                    if (data := self._receipt_data(path))]
        if not any(data.get("status") == "completed" and data.get("skill") == skill_id
                   and data.get("request_id") == request_id and data.get("event") != "feedback"
                   for _, data in receipts):
            raise ValueError("No completed receipt for this skill and request ID")
        if any(data.get("event") == "feedback" and data.get("request_id") == request_id
               for _, data in receipts):
            raise ValueError("Feedback has already been recorded for this run")
        return self.receipt(f"{request_id}-feedback", {"event": "feedback", "request_id": request_id,
                                                         "skill": skill_id, "status": "successful" if useful else "unsuccessful",
                                                         **({"skill_hash": skill_hash} if skill_hash else {})})

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

    METRICS_HISTORY_LIMIT = 60

    def record_metrics_snapshot(self) -> None:
        """Append a timestamped snapshot of current metrics for sparklines."""
        state = self.load_state()
        history = state.setdefault("metrics_history", [])
        snapshot = {"at": now().isoformat(), "values": {}}
        for entry in self.metrics():
            for metric in entry["metrics"]:
                try:
                    snapshot["values"][f"{entry['name']}:{metric['key']}"] = float(str(metric["value"]).replace(",", ""))
                except (TypeError, ValueError):
                    continue
        if not snapshot["values"]:
            return
        history.append(snapshot)
        del history[:-self.METRICS_HISTORY_LIMIT]
        self.write_state(state)

    def metrics_history(self, limit: int = 60) -> list[dict]:
        return self.load_state().get("metrics_history", [])[-limit:]

    def metrics(self) -> list[dict]:
        folder = self.root / "metrics"
        if not folder.exists():
            return []
        entries = []
        for path in sorted(p for p in folder.glob("**/*") if p.is_file() and p.suffix.lower() in {".toml", ".md"}):
            if path.name.lower() == "readme.md":
                continue
            name = path.stem.replace("-", " ").replace("_", " ").title()
            if path.suffix.lower() == ".toml":
                import tomllib
                try:
                    data = tomllib.loads(path.read_text(encoding="utf-8"))
                    metrics = data.get("metrics", []) if data.get("schema_version") == 1 else []
                    metrics = [{"key": str(item.get("key", "")), "value": str(item.get("value", "")),
                                "unit": str(item.get("unit", "")), "updated": str(item.get("updated", "")),
                                "source": str(item.get("source", ""))}
                               for item in metrics if isinstance(item, dict)]
                except (ValueError, OSError):
                    metrics = []
            else:
                text = path.read_text(encoding="utf-8")
                metrics = []
                for line in text.splitlines():
                    match = re.match(r"\s*[-*]\s*([^:]+):\s*(.*)$", line)
                    if match:
                        key, value = match.groups()
                        metrics.append({"key": key.strip(), "value": value.strip(), "unit": "", "updated": "", "source": ""})
            entries.append({"name": name, "path": str(path.relative_to(self.root)), "metrics": metrics})
        return entries

    NON_EXECUTION_EVENTS = {"feedback", "skill_test", "promotion", "approval_request",
                            "approval_decision", "skill_rollback", "scheduled_run"}

    @staticmethod
    def _receipt_at(path: Path) -> str | None:
        match = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", path.stem)
        if not match:
            return None
        year, month, day, hour, minute, second = match.groups()
        return f"{year}-{month}-{day} {hour}:{minute}:{second}"

    def receipt_index(self, per_skill_limit: int = 15) -> dict[str, dict]:
        """Single pass over receipts -> per-skill evidence buckets.

        Buckets keep only bounded metadata (no instructions, no secret values):
        feedback (keyed by request id, with the skill hash recorded at run
        time), execution receipts, and declared-test receipts.
        """
        index: dict[str, dict] = {}
        for path in sorted((self.root / "receipts").glob("**/*.md")):
            data = self._receipt_data(path)
            skill = data.get("skill")
            if not skill or not isinstance(skill, str):
                continue
            bucket = index.setdefault(skill, {"feedback": {}, "executions": [], "tests": []})
            event = data.get("event")
            at = self._receipt_at(path)
            if event == "feedback":
                bucket["feedback"][str(data.get("request_id", path.stem))] = {
                    "status": data.get("status", ""), "skill_hash": data.get("skill_hash"), "at": at}
            elif event == "skill_test":
                bucket["tests"].append({"request_id": str(data.get("request_id", path.stem)),
                                        "status": data.get("status"), "skill_hash": data.get("skill_hash"),
                                        "at": at, "passed": data.get("passed"), "total": data.get("total")})
                bucket["tests"] = bucket["tests"][-per_skill_limit:]
            elif event not in self.NON_EXECUTION_EVENTS and data.get("status") in {"completed", "error"}:
                bucket["executions"].append({"request_id": str(data.get("request_id", path.stem)),
                                             "task_id": data.get("task_id"), "status": data.get("status"),
                                             "tier": data.get("tier"), "at": at,
                                             "output": data.get("output"),
                                             "error": (str(data.get("error"))[:300] or None) if data.get("error") else None})
                bucket["executions"] = bucket["executions"][-per_skill_limit:]
        return index

    def skill_stats(self, skill_id: str, *, current_hash: str | None = None,
                    baseline_hash: str | None = None, index: dict | None = None) -> dict:
        """Per-skill run evidence, optionally scoped to a skill version.

        With current_hash set, only feedback recorded for that exact version
        counts; receipts without a recorded hash are legacy and count only
        while the skill is unchanged from its recorded baseline. Runs for
        other versions are reported as stale and cannot certify the current
        code for automation review.
        """
        if index is None:
            index = self.receipt_index()
        bucket = index.get(skill_id, {})
        successful = failed = legacy_successful = legacy_failed = stale = 0
        for item in bucket.get("feedback", {}).values():
            status = item.get("status")
            recorded = item.get("skill_hash")
            if current_hash and recorded and recorded != current_hash:
                stale += 1
                continue
            if current_hash and not recorded:
                if not (baseline_hash and baseline_hash == current_hash):
                    stale += 1
                    continue
                legacy_successful += status == "successful"
                legacy_failed += status == "unsuccessful"
                continue
            successful += status == "successful"
            failed += status == "unsuccessful"
        total_success = successful + legacy_successful
        total_failed = failed + legacy_failed
        promotion = self.promotion(skill_id)
        stats = {
            "successful_runs": total_success,
            "error_runs": total_failed,
            "automation_review_due": total_success >= 5,
            "promotion_status": promotion.get("status", "not_started"),
            "recent_runs": list(reversed(bucket.get("executions", [])))[:10],
        }
        tests = bucket.get("tests", [])
        if current_hash:
            scoped_tests = [item for item in tests if item.get("skill_hash") == current_hash]
            latest = scoped_tests[-1] if scoped_tests else None
            stats["evidence"] = {
                "hash": current_hash,
                "baseline_hash": baseline_hash,
                "legacy_runs_counted": (legacy_successful + legacy_failed) > 0,
                "stale_runs": stale,
                "test": {"status": latest.get("status") or "unknown", "at": latest.get("at"),
                         "passed": latest.get("passed"), "total": latest.get("total")} if latest else None,
            }
        elif tests:
            latest = tests[-1]
            stats["test_evidence"] = {"status": latest.get("status") or "unknown", "at": latest.get("at"),
                                      "passed": latest.get("passed"), "total": latest.get("total")}
        return stats

    def promote_skill(self, skill_id: str, *, stats: dict | None = None, skill_hash: str | None = None) -> dict:
        stats = stats or self.skill_stats(skill_id)
        current = self.promotion(skill_id)
        if stats["successful_runs"] < 5:
            raise ValueError("Five user-confirmed successful runs are required")
        if current["status"] in {"pending_review", "approved"} and (not skill_hash or current.get("skill_hash") == skill_hash):
            return current
        history = current.get("history", [])
        history.append({"status": "pending_review", "at": now().isoformat(),
                        **({"skill_hash": skill_hash} if skill_hash else {})})
        data = {"event": "promotion", "skill": skill_id, "status": "pending_review", "history": history,
                "skill_hash": skill_hash,
                "evidence": {"successful_runs": stats["successful_runs"], "error_runs": stats["error_runs"],
                             "test": (stats.get("evidence") or {}).get("test")}}
        self._write_promotion(skill_id, data)
        return data

    def approve_promotion(self, skill_id: str, *, skill_hash: str | None = None) -> dict:
        current = self.promotion(skill_id)
        if current["status"] != "pending_review":
            raise ValueError("Skill must be pending automation review")
        recorded = skill_hash or current.get("skill_hash")
        history = current.get("history", [])
        history.append({"status": "approved", "at": now().isoformat(),
                        **({"skill_hash": recorded} if recorded else {})})
        data = {"event": "promotion", "skill": skill_id, "status": "approved", "history": history}
        if recorded:
            data["skill_hash"] = recorded
        if current.get("evidence"):
            data["evidence"] = current["evidence"]
        self._write_promotion(skill_id, data)
        return data
