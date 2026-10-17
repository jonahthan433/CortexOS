"""Server-side connector state: OAuth client configs, tokens, idempotency
journal, and per-process rate limiting. Tokens and client secrets never leave
these files; nothing here is ever returned by an API or written to receipts.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

from .base import ConnectorValidationError


def _write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".conn-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ConnectorStore:
    def __init__(self, root: Path, client_dir: Path):
        self.root = root
        self.client_dir = client_dir

    def client_path(self, connector_id: str) -> Path:
        return self.client_dir / f"{connector_id}.json"

    def client_config(self, connector_id: str) -> dict | None:
        try:
            data = json.loads(self.client_path(connector_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("client_id"), str) or not data["client_id"]:
            return None
        if data.get("client_secret") is not None and not isinstance(data["client_secret"], str):
            return None
        return data

    def client_mode(self, connector_id: str) -> int | None:
        try:
            return stat.S_IMODE(self.client_path(connector_id).stat().st_mode)
        except OSError:
            return None

    def redirect_uri(self, connector_id: str) -> str:
        config = self.client_config(connector_id) or {}
        return str(config.get("redirect_uri") or "http://localhost:8765/connectors/callback")

    def token_path(self, connector_id: str) -> Path:
        return self.root / connector_id / "token.json"

    def token(self, connector_id: str) -> dict | None:
        try:
            data = json.loads(self.token_path(connector_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save_token(self, connector_id: str, data: dict) -> None:
        _write_private(self.token_path(connector_id), json.dumps(data, indent=2, sort_keys=True) + "\n")

    def delete_token(self, connector_id: str) -> bool:
        path = self.token_path(connector_id)
        if not path.exists():
            return False
        path.unlink()
        return True


class IdempotencyJournal:
    """Dedupes external side effects: the same request is never sent twice.

    Entry states: pending -> completed | failed | unknown. ``failed`` means the
    API confirmed nothing happened and the key may be attempted again;
    ``unknown`` (timeout or 5xx after an external call) blocks silent retries
    until a human inspects the outcome.
    """

    def __init__(self, path: Path, limit: int = 500):
        self.path = path
        self.limit = limit

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    def _write(self, entries: dict) -> None:
        if len(entries) > self.limit:
            ordered = sorted(entries.items(), key=lambda pair: pair[1].get("at", 0))
            entries = dict(ordered[-self.limit:])
        _write_private(self.path, json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n")

    def lookup(self, key: str) -> dict | None:
        return self._load().get(key)

    def begin(self, key: str, connector: str, operation: str) -> None:
        entries = self._load()
        entries[key] = {"status": "pending", "at": time.time(), "connector": connector, "operation": operation}
        self._write(entries)

    def finish(self, key: str, *, status: str, result_ref: str | None = None) -> None:
        entries = self._load()
        entry = entries.get(key) or {"connector": "", "operation": ""}
        entry.update({"status": status, "finished_at": time.time()})
        if result_ref:
            entry["result_ref"] = result_ref
        entries[key] = entry
        self._write(entries)


class RateLimiter:
    """In-process sliding-window limiter, keyed per connector operation."""

    def __init__(self):
        self._marks: dict[str, list[float]] = {}

    def check(self, key: str, limit_per_minute: int, now: float | None = None) -> float | None:
        if limit_per_minute <= 0:
            return None
        now = time.time() if now is None else now
        window_start = now - 60.0
        marks = [mark for mark in self._marks.get(key, []) if mark > window_start]
        if len(marks) >= limit_per_minute:
            retry_after = 60.0 - (now - marks[0])
            self._marks[key] = marks
            return max(0.1, retry_after)
        marks.append(now)
        self._marks[key] = marks
        return None


def validate_connector_id(connector_id: str, known: set[str]) -> str:
    if connector_id not in known:
        raise ConnectorValidationError(f"Unknown connector: {connector_id}")
    return connector_id
