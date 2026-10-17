"""Read-only browsing adapters for the HUD.

Two concerns, both strictly read-only:

- ``VaultBrowser``: jailed access to the configured Obsidian vault (tree,
  file preview, search). Absolute paths, traversal, symlink escapes, hidden
  entries, and ``state.json`` are rejected. Nothing outside the vault is
  reachable, and no write operation exists by design.
- ``search_activity`` / ``agents_directory``: honest aggregation over data
  that already exists (durable queue SQLite, vault request records, role
  agent specs). No invented rows: empty stays empty, planned agents are
  labeled as such.
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".tar", ".mp3", ".wav", ".ogg", ".opus", ".mp4", ".webm", ".mov", ".sqlite", ".db",
    ".pyc", ".woff", ".woff2", ".ttf", ".otf", ".bin", ".so", ".key", ".crt",
}
DENIED_NAMES = {"state.json"}
MAX_TREE_ENTRIES = 500
MAX_FILE_BYTES = 200_000
MAX_SEARCH_FILES = 400
MAX_SEARCH_BYTES = 8_000_000
PROMPT_EXCERPT = 140


class BrowseError(ValueError):
    """Path refused by the vault jail or missing."""


class VaultBrowser:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ jail
    def _resolve(self, rel: str) -> Path:
        if rel is None or not isinstance(rel, str):
            raise BrowseError("path is required")
        raw = rel.strip()
        if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
            raise BrowseError("absolute paths are not allowed")
        parts = Path(raw).parts if raw not in ("", ".") else ()
        if any(part == ".." for part in parts):
            raise BrowseError("path traversal is not allowed")
        target = (self.root / raw).resolve() if raw not in ("", ".") else self.root
        if target != self.root and not target.is_relative_to(self.root):
            raise BrowseError("path escapes the vault")
        for part in target.relative_to(self.root).parts:
            if part.startswith(".") or part in DENIED_NAMES:
                raise BrowseError("access denied for this entry")
        return target

    @staticmethod
    def _entry(path: Path, relative: str) -> dict:
        is_dir = path.is_dir()
        try:
            stat = path.stat()
            size, modified = stat.st_size, stat.st_mtime
        except OSError:
            size, modified = 0, 0
        return {"name": path.name, "path": relative, "type": "dir" if is_dir else "file",
                "size": size, "modified": modified}

    # ------------------------------------------------------------------ tree
    def tree(self, rel: str = "") -> dict:
        target = self._resolve(rel)
        if not target.exists():
            raise BrowseError("folder not found")
        if not target.is_dir():
            raise BrowseError("not a folder")
        entries: list[dict] = []
        for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if child.name.startswith(".") or child.name in DENIED_NAMES:
                continue
            relative = str(child.relative_to(self.root))
            entries.append(self._entry(child, relative))
        truncated = len(entries) > MAX_TREE_ENTRIES
        return {"path": rel or ".", "entries": entries[:MAX_TREE_ENTRIES], "truncated": truncated}

    # ------------------------------------------------------------------ file
    def file(self, rel: str) -> dict:
        target = self._resolve(rel)
        if not target.exists():
            raise BrowseError("file not found")
        if target.is_dir():
            raise BrowseError("path is a folder")
        size = target.stat().st_size
        payload: dict[str, Any] = {
            "path": str(target.relative_to(self.root)),
            "size": size,
            "modified": target.stat().st_mtime,
            "binary": False,
            "truncated": False,
            "content_type": "text/markdown" if target.suffix.lower() == ".md" else "text/plain",
        }
        if target.suffix.lower() in BINARY_SUFFIXES:
            payload["binary"] = True
            return payload
        with open(target, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if b"\x00" in raw[:8192]:
            payload["binary"] = True
            return payload
        payload["truncated"] = len(raw) > MAX_FILE_BYTES
        payload["text"] = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        return payload

    # ---------------------------------------------------------------- search
    def search(self, query: str, limit: int = 50) -> dict:
        needle = (query or "").strip().casefold()
        if not needle:
            raise BrowseError("search query is required")
        limit = max(1, min(int(limit), 100))
        results: list[dict] = []
        scanned_bytes = 0
        scanned_files = 0
        truncated = False
        for path in sorted(self.root.rglob("*")):
            if scanned_files >= MAX_SEARCH_FILES or scanned_bytes >= MAX_SEARCH_BYTES:
                truncated = True
                break
            if not path.is_file():
                continue
            relative_parts = path.relative_to(self.root).parts
            if any(part.startswith(".") or part in DENIED_NAMES for part in relative_parts):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned_files += 1
            scanned_bytes += len(text)
            relative = str(path.relative_to(self.root))
            if needle in path.name.casefold():
                results.append({"path": relative, "where": "name", "line": None, "snippet": ""})
            elif needle in text.casefold():
                index = text.casefold().find(needle)
                line = text.count("\n", 0, index) + 1
                snippet = text[max(0, index - 60): index + len(needle) + 60].replace("\n", " ")
                results.append({"path": relative, "where": "content", "line": line, "snippet": snippet.strip()})
            if len(results) >= limit:
                truncated = True
                break
        return {"query": query, "results": results, "truncated": truncated,
                "scanned_files": scanned_files}


# ---------------------------------------------------------------- activity
def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _excerpt(text: str, length: int = PROMPT_EXCERPT) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:length] + ("…" if len(compact) > length else "")


def _request_record(path: Path, query: str) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return None
    meta = {}
    for match in re.finditer(r"^(\w+):\s*(.+)$", text[:1200], flags=re.M):
        meta.setdefault(match.group(1), match.group(2).strip())
    created = meta.get("created", "")
    try:
        at = datetime.fromisoformat(created).timestamp()
    except ValueError:
        at = path.stat().st_mtime
    body = text.split("---", 2)[2] if text.count("---") >= 2 else text
    prompt = ""
    lines = [line.strip() for line in body.splitlines()]
    for index, line in enumerate(lines):
        if line.startswith("# Request"):
            for following in lines[index + 1:]:
                if following and not following.startswith("#"):
                    prompt = following
                    break
            break
    if not prompt:
        prompt = next((line for line in lines if line and not line.startswith("#")), "")
    if query and query.casefold() not in text.casefold():
        return None
    return {"kind": "request", "id": meta.get("id", path.stem), "title": _excerpt(prompt),
            "status": meta.get("status", "unknown"), "at": at, "ref": f"requests/{path.name}",
            "detail": f"tier {meta.get('tier', '?')} · {meta.get('backend', '?')}"}


def search_activity(queue_db: Path, vault_root: Path, *, q: str = "", kind: str = "all",
                    status: str = "", limit: int = 20, before: float | None = None) -> dict:
    """Merged, time-ordered activity over queue tasks, task events, and vault
    request records. Pagination uses a ``before`` timestamp cursor so merged
    results stay correct across sources."""
    kind = kind if kind in {"all", "task", "event", "request"} else "all"
    status = status or ""
    limit = max(1, min(int(limit), 100))
    needle = (q or "").strip().casefold()
    items: list[dict] = []
    sources = {"task": 0, "event": 0, "request": 0}
    fetch = limit + 1  # one extra row per source detects "has more" honestly

    conn = _connect_ro(Path(queue_db)) if kind in {"all", "task", "event"} else None
    if conn is not None:
        try:
            if kind in {"all", "task"}:
                rows = conn.execute(
                    "SELECT id, prompt, backend, skill_id, status, priority, error, result_ref, created_at, finished_at "
                    "FROM tasks WHERE (? = '' OR lower(prompt) LIKE ?) AND (? = '' OR status = ?) "
                    "AND (? IS NULL OR created_at <= ?) ORDER BY created_at DESC LIMIT ?",
                    (needle, f"%{needle}%", status, status, before, before, fetch)).fetchall()
                for row in rows:
                    sources["task"] += 1
                    items.append({"kind": "task", "id": row["id"], "title": _excerpt(row["prompt"]),
                                  "status": row["status"], "priority": row["priority"], "at": row["created_at"],
                                  "backend": row["backend"], "skill": row["skill_id"],
                                  "detail": row["error"] or row["result_ref"] or "",
                                  "ref": f"/api/tasks/{row['id']}"})
            if kind in {"all", "event"}:
                rows = conn.execute(
                    "SELECT e.seq, e.task_id, e.at, e.kind, e.message, t.prompt FROM task_events e "
                    "LEFT JOIN tasks t ON t.id = e.task_id "
                    "WHERE (? = '' OR lower(coalesce(e.message, '')) LIKE ? OR lower(e.kind) LIKE ?) "
                    "AND (? IS NULL OR e.at <= ?) ORDER BY e.at DESC LIMIT ?",
                    (needle, f"%{needle}%", f"%{needle}%", before, before, fetch)).fetchall()
                for row in rows:
                    sources["event"] += 1
                    items.append({"kind": "event", "id": f"{row['task_id']}:{row['seq']}", "task_id": row["task_id"],
                                  "title": f"{row['kind']}: {row['message']}" if row["message"] else row["kind"],
                                  "status": "", "at": row["at"], "backend": None, "skill": None,
                                  "detail": _excerpt(row["prompt"] or "", 90), "ref": f"/api/tasks/{row['task_id']}"})
        finally:
            conn.close()

    if kind in {"all", "request"} and Path(vault_root).exists():
        request_folder = Path(vault_root) / "requests"
        for path in sorted(request_folder.glob("*.md"), reverse=True):
            if sources["request"] > limit:
                break
            record = _request_record(path, needle)
            if record is None:
                continue
            if status and record["status"] != status:
                continue
            if before is not None and record["at"] > before:
                continue
            sources["request"] += 1
            items.append(record)

    items.sort(key=lambda item: item["at"], reverse=True)
    merged = items[:limit]
    has_more = len(items) > limit
    return {"items": merged, "has_more": has_more,
            "cursor": merged[-1]["at"] if merged and has_more else None,
            "sources": {name: min(count, limit) for name, count in sources.items()}}


def recent_tasks(queue_db: Path, *, backend: str | None = None, skill: str | None = None,
                 limit: int = 3) -> list[dict]:
    conn = _connect_ro(Path(queue_db))
    if conn is None:
        return []
    try:
        if backend:
            rows = conn.execute(
                "SELECT id, prompt, status, created_at FROM tasks WHERE backend = ? "
                "ORDER BY created_at DESC LIMIT ?", (backend, limit)).fetchall()
        elif skill:
            rows = conn.execute(
                "SELECT id, prompt, status, created_at FROM tasks WHERE skill_id = ? "
                "ORDER BY created_at DESC LIMIT ?", (skill, limit)).fetchall()
        else:
            rows = []
        return [{"id": row["id"], "title": _excerpt(row["prompt"], 90), "status": row["status"],
                 "at": row["created_at"]} for row in rows]
    finally:
        conn.close()


def agents_directory(settings, coordinator, gateway, queue_db: Path, recent: int = 3) -> list[dict]:
    """One honest list: CLI execution adapters, model-gateway agents, and
    skill-backed role agents — implemented versus planned labeled explicitly."""
    from .agents import AGENT_SPECS

    items: list[dict] = []
    for agent in settings.agents:
        assignment = gateway.resolve(agent["id"])
        entry = {
            "id": agent["id"], "name": agent.get("name", agent["id"]), "kind": "cli",
            "role": "execution adapter", "lane": "local process", "implemented": True,
            "capabilities": ["cli-execution", "live streaming", "cancellation"],
            "detail": "CLI coding agent. Assign a provider in Settings to run it through the model gateway instead.",
            "assignment": {"provider": assignment.provider, "model": assignment.model} if assignment else None,
            "issues": [],
            "recent": recent_tasks(queue_db, backend=agent["id"], limit=recent),
        }
        items.append(entry)

    for entry in gateway.agents_status():
        provider = gateway.providers.get(entry["provider"])
        items.append({
            "id": entry["agent_id"], "name": entry["name"], "kind": "llm",
            "role": "model agent", "lane": "model gateway", "implemented": True,
            "capabilities": sorted(name for name, on in (provider.capabilities if provider else {}).items() if on),
            "detail": f"Runs via provider '{entry['provider']}' ({provider.kind if provider else 'unknown'})"
                      + (f" · fallback: {', '.join(item['provider'] for item in entry['fallback'])}" if entry["fallback"] else ""),
            "assignment": {"provider": entry["provider"], "model": entry["model"]},
            "issues": entry.get("issues", []),
            "recent": recent_tasks(queue_db, backend=entry["agent_id"], limit=recent),
        })

    implemented = getattr(coordinator, "agents", {})
    for spec in AGENT_SPECS:
        agent = implemented.get(spec["id"])
        items.append({
            "id": spec["id"], "name": agent.name if agent else spec["id"], "kind": "role",
            "role": spec["role"], "lane": spec["lane"], "autonomous": spec["autonomous"],
            "implemented": agent is not None,
            "capabilities": ["skill-execution"] if agent else [],
            "detail": "Skill-backed role agent; runs through the durable queue."
                      if agent else "Planned: skill manifest is not installed, so it cannot run yet.",
            "assignment": None, "issues": [],
            "recent": recent_tasks(queue_db, skill=spec["id"], limit=recent) if agent else [],
        })
    return items
