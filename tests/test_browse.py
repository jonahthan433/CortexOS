"""Vault jail, vault search, activity search, and the agents directory.

Everything runs against temporary folders/databases: no live vault, no live
queue, no network. The jail tests are the security-critical ones.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cortexos.browse import VaultBrowser, BrowseError, agents_directory, recent_tasks, search_activity
from cortexos.config import Settings
from cortexos.llm import Assignment, ModelGateway


def make_vault(tmp_path) -> Path:
    root = tmp_path / "vault"
    for folder in ("raw", "wiki", "output", "requests", "receipts/2026/10", "metrics"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "note.md").write_text("# Note\n\nThe quick brown fox jumps.\n", encoding="utf-8")
    (root / "raw" / "capture.txt").write_text("capture text", encoding="utf-8")
    (root / "state.json").write_text("{}", encoding="utf-8")
    (root / ".hidden.md").write_text("secret", encoding="utf-8")
    (root / "metrics" / "usage.toml").write_text("[usage]\n", encoding="utf-8")
    return root


def make_queue_db(tmp_path) -> Path:
    db = tmp_path / "state" / "queue.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, origin TEXT, prompt TEXT NOT NULL, backend TEXT,
        skill_id TEXT, priority INTEGER NOT NULL DEFAULT 5, status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, cycle INTEGER NOT NULL DEFAULT 0,
        request_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, next_attempt_at REAL, started_at REAL,
        finished_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0, worker TEXT, lease_expires_at REAL,
        error TEXT, retryable INTEGER, result_ref TEXT)""")
    conn.execute("""CREATE TABLE task_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        at REAL NOT NULL, kind TEXT NOT NULL, message TEXT)""")
    def task(task_id, prompt, status, at, backend="codex", skill=None, error=None, result=None):
        conn.execute("INSERT INTO tasks (id, prompt, status, created_at, updated_at, backend, skill_id, error, result_ref) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (task_id, prompt, status, at, at, backend, skill, error, result))
    task("t1", "summarize the weekly report", "completed", 100.0, result="vault/output/a.md")
    task("t2", "delete old receipts", "failed", 200.0, error="approval required")
    task("t3", "morning intel brief", "queued", 300.0, backend="hermes", skill="research-morning-intel")
    conn.execute("INSERT INTO task_events (task_id, at, kind, message) VALUES ('t1', 150.0, 'claimed', 'worker-1 picked it up')")
    conn.execute("INSERT INTO task_events (task_id, at, kind, message) VALUES ('t2', 250.0, 'failed', 'terminal error')")
    conn.commit()
    conn.close()
    return db


def write_request(root: Path, request_id: str, prompt: str, status: str, created: str, tier: int = 3, backend: str = "codex"):
    path = root / "requests" / f"{request_id}.md"
    path.write_text(
        f"---\nid: {request_id}\ncreated: {created}\nstatus: {status}\ntier: {tier}\nbackend: {backend}\n---\n\n"
        f"# Request\n\n{prompt}\n\n## Update — later\n\nnothing\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ jail
def test_jail_rejects_absolute_traversal_and_windows_paths(tmp_path):
    browser = VaultBrowser(make_vault(tmp_path))
    for bad in ("/etc/passwd", "../outside.txt", "wiki/../../etc", "C:\\Windows\\system32", "\\\\host\\share"):
        with pytest.raises(BrowseError):
            browser.file(bad)
    with pytest.raises(BrowseError):
        browser.tree("..")


def test_jail_rejects_hidden_and_denied_entries(tmp_path):
    root = make_vault(tmp_path)
    browser = VaultBrowser(root)
    with pytest.raises(BrowseError):
        browser.file(".hidden.md")
    with pytest.raises(BrowseError):
        browser.file("state.json")
    (root / "receipts" / ".private").write_text("x", encoding="utf-8")
    with pytest.raises(BrowseError):
        browser.file("receipts/.private")


def test_jail_rejects_symlink_escape(tmp_path):
    root = make_vault(tmp_path)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("top secret", encoding="utf-8")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "data.md").write_text("more secrets", encoding="utf-8")
    (root / "leak.txt").symlink_to(outside_file)
    (root / "leakdir").symlink_to(outside_dir)
    browser = VaultBrowser(root)
    with pytest.raises(BrowseError):
        browser.file("leak.txt")
    with pytest.raises(BrowseError):
        browser.tree("leakdir")
    with pytest.raises(BrowseError):
        browser.file("leakdir/data.md")
    # an internal symlink stays usable
    (root / "wiki-link.md").symlink_to(root / "wiki" / "note.md")
    assert "quick brown fox" in browser.file("wiki-link.md")["text"]


def test_tree_lists_dirs_first_and_hides_denied(tmp_path):
    browser = VaultBrowser(make_vault(tmp_path))
    listing = browser.tree("")
    names = [entry["name"] for entry in listing["entries"]]
    assert names[0] in {"metrics", "raw", "receipts", "requests", "wiki"}  # dirs before files
    assert "state.json" not in names and ".hidden.md" not in names
    types = {entry["name"]: entry["type"] for entry in listing["entries"]}
    assert types["wiki"] == "dir"
    note = browser.tree("wiki")["entries"]
    assert note[0]["name"] == "note.md" and note[0]["size"] > 0
    with pytest.raises(BrowseError):
        browser.tree("wiki/note.md")  # a file is not a folder


def test_file_reads_text_and_flags_binary_and_truncation(tmp_path):
    root = make_vault(tmp_path)
    big = root / "output" / "big.md"
    big.write_text("x" * 250_000, encoding="utf-8")
    blob = root / "output" / "image.png"
    blob.write_bytes(b"\x89PNG" + b"\x00" * 64)
    nul = root / "output" / "weird.md"
    nul.write_bytes(b"abc\x00def")
    browser = VaultBrowser(root)
    note = browser.file("wiki/note.md")
    assert note["binary"] is False and note["truncated"] is False
    assert note["content_type"] == "text/markdown"
    assert browser.file("output/big.md")["truncated"] is True
    assert len(browser.file("output/big.md")["text"]) == 200_000
    assert browser.file("output/image.png")["binary"] is True
    assert "text" not in browser.file("output/image.png")
    assert browser.file("output/weird.md")["binary"] is True
    with pytest.raises(BrowseError):
        browser.file("wiki/missing.md")
    with pytest.raises(BrowseError):
        browser.file("wiki")


def test_search_matches_names_and_content_and_skips_denied(tmp_path):
    root = make_vault(tmp_path)
    (root / ".hidden.md").write_text("quick brown fox hideout", encoding="utf-8")
    browser = VaultBrowser(root)
    by_name = browser.search("note")
    assert any(item["where"] == "name" and item["path"] == "wiki/note.md" for item in by_name["results"])
    by_content = browser.search("brown fox")
    content_hits = [item for item in by_content["results"] if item["where"] == "content"]
    assert content_hits and content_hits[0]["path"] == "wiki/note.md" and content_hits[0]["line"] == 3
    assert all(".hidden.md" not in item["path"] for item in browser.search("hideout")["results"])
    assert browser.search("nothing-matches-this")["results"] == []
    with pytest.raises(BrowseError):
        browser.search("   ")


# --------------------------------------------------------------- activity
def test_activity_search_merges_sources_and_filters(tmp_path):
    db = make_queue_db(tmp_path)
    root = make_vault(tmp_path)
    write_request(root, "r1", "write the client brief", "completed", "1970-01-01T00:02:20+00:00")
    write_request(root, "r2", "archive the old receipts", "rejected", "1970-01-01T00:04:00+00:00")

    everything = search_activity(db, root, limit=20)
    kinds = {item["kind"] for item in everything["items"]}
    assert kinds == {"task", "event", "request"}
    times = [item["at"] for item in everything["items"]]
    assert times == sorted(times, reverse=True)
    request_item = next(item for item in everything["items"] if item["kind"] == "request" and item["id"] == "r1")
    assert request_item["status"] == "completed" and "client brief" in request_item["title"]

    tasks_only = search_activity(db, root, kind="task", q="receipts")
    assert [item["id"] for item in tasks_only["items"]] == ["t2"]
    events_only = search_activity(db, root, kind="event", q="worker")
    assert events_only["items"][0]["task_id"] == "t1"
    failed = search_activity(db, root, kind="task", status="failed")
    assert [item["id"] for item in failed["items"]] == ["t2"]
    requests_only = search_activity(db, root, kind="request", status="rejected")
    assert [item["id"] for item in requests_only["items"]] == ["r2"]
    assert search_activity(db, root, q="no-such-thing")["items"] == []


def test_activity_pagination_cursor_covers_timeline(tmp_path):
    db = make_queue_db(tmp_path)
    root = make_vault(tmp_path)
    write_request(root, "r1", "write the client brief", "completed", "1970-01-01T00:02:20+00:00")
    write_request(root, "r2", "archive the old receipts", "rejected", "1970-01-01T00:04:00+00:00")
    page1 = search_activity(db, root, limit=2)
    assert len(page1["items"]) == 2 and page1["has_more"] is True and page1["cursor"] is not None
    page2 = search_activity(db, root, limit=10, before=page1["cursor"])
    assert page2["items"]
    overlap = {item["id"] for item in page1["items"]} & {item["id"] for item in page2["items"]}
    # the boundary timestamp may repeat; clients dedupe by id. nothing newer may leak in.
    assert all(item["at"] <= page1["cursor"] for item in page2["items"])
    combined = {item["id"] for item in page1["items"]} | {item["id"] for item in page2["items"]}
    assert {"t1", "t2", "t3", "r1", "r2"} <= combined


def test_activity_tolerates_missing_database(tmp_path):
    root = make_vault(tmp_path)
    write_request(root, "r9", "still visible", "completed", "2026-10-17T00:00:00+03:00")
    result = search_activity(tmp_path / "state" / "missing.db", root)
    assert [item["id"] for item in result["items"]] == ["r9"]


def test_recent_tasks_filters_by_backend_and_skill(tmp_path):
    db = make_queue_db(tmp_path)
    assert [item["id"] for item in recent_tasks(db, backend="hermes")] == ["t3"]
    assert [item["id"] for item in recent_tasks(db, skill="research-morning-intel")] == ["t3"]
    assert recent_tasks(db, backend="ghost") == []
    assert recent_tasks(tmp_path / "missing.db", backend="codex") == []


# ------------------------------------------------------------- directory
def make_gateway(tmp_path, providers):
    handler = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    settings = Settings(root=tmp_path, llm_providers=providers)
    return ModelGateway(settings, transport=handler, assignment_path=tmp_path / "llm_assignments.json")


def test_agents_directory_labels_implemented_and_planned(tmp_path):
    db = make_queue_db(tmp_path)
    providers = {
        "acme": {"kind": "openai_compatible", "base_url": "https://api.acme.test/v1",
                 "api_key": "sk-x", "default_model": "acme-1"},
    }
    gateway = make_gateway(tmp_path, providers)
    gateway.save_assignment("gemini-flash", {"provider": "acme", "model": "acme-1", "name": "Flash"})
    settings = SimpleNamespace(agents=[{"id": "codex", "name": "Codex"},
                                       {"id": "hermes", "name": "Hermes"}])
    coordinator = SimpleNamespace(agents={"research-morning-intel": SimpleNamespace(name="Morning Intel")})

    directory = agents_directory(settings, coordinator, gateway, db)
    by_id = {entry["id"]: entry for entry in directory}

    assert by_id["codex"]["kind"] == "cli" and by_id["codex"]["implemented"] is True
    assert "cli-execution" in by_id["codex"]["capabilities"]
    assert [item["id"] for item in by_id["codex"]["recent"]] == ["t2", "t1"]
    assert by_id["gemini-flash"]["kind"] == "llm" and by_id["gemini-flash"]["issues"] == []
    assert by_id["gemini-flash"]["assignment"] == {"provider": "acme", "model": "acme-1"}
    assert "chat" in by_id["gemini-flash"]["capabilities"]
    assert by_id["research-morning-intel"]["implemented"] is True
    assert by_id["research-morning-intel"]["recent"][0]["id"] == "t3"
    assert by_id["backup-restore"]["implemented"] is False
    assert "Planned" in by_id["backup-restore"]["detail"]
    assert all(entry["recent"] is not None for entry in directory)


def test_agents_directory_marks_invalid_assignment_issues(tmp_path):
    db = make_queue_db(tmp_path)
    gateway = make_gateway(tmp_path, {})
    gateway.assignments["broken"] = Assignment(
        agent_id="broken", provider="ghost-provider", model="m")
    settings = SimpleNamespace(agents=[])
    coordinator = SimpleNamespace(agents={})
    directory = agents_directory(settings, coordinator, gateway, db)
    broken = next(entry for entry in directory if entry["id"] == "broken")
    assert broken["issues"] and "unknown provider" in broken["issues"][0]
    assert broken["capabilities"] == []
