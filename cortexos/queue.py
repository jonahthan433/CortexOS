"""Durable task queue for CortexOS.

SQLite (WAL) is the operational source of truth for task lifecycle; the
Obsidian vault keeps request receipts and deliverables. Everyone executes the
queue through the same workers, which are asyncio tasks inside the Bridge, so
a task still marked ``running`` after a restart belongs to a dead process and
is recovered explicitly.

Honesty notes:

* Exactly-once execution is **not** guaranteed. An interrupted attempt can be
  replayed after a restart; dedupe keys, per-attempt request ids, and a
  vault-level idempotency check minimize duplicate side effects.
* Retries are bounded (``max_attempts``) and scheduled with exponential
  backoff plus jitter; a task that exhausts attempts lands in ``failed`` and
  stays until a human retries or retention prunes it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .vault import safe_name

logger = logging.getLogger("cortexos.queue")

SCHEMA_VERSION = 1
ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ALL_STATUSES = (*ACTIVE_STATUSES, *TERMINAL_STATUSES)


class QueueError(RuntimeError):
    pass


def _row_to_task(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    task = dict(row)
    try:
        task["meta"] = json.loads(task.get("meta") or "{}")
    except ValueError:
        task["meta"] = {}
    if task.get("retryable") is not None:
        task["retryable"] = bool(task["retryable"])
    return task


class QueueStore:
    """Atomic, thread-safe SQLite task store. All operations are short and synchronous."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, mode=0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=15,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with contextlib.suppress(OSError):
            os.chmod(self.db_path, 0o600)
        self._migrate()

    # ------------------------------------------------------------------ setup

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY,
                  origin TEXT,
                  prompt TEXT NOT NULL,
                  backend TEXT,
                  skill_id TEXT,
                  priority INTEGER NOT NULL DEFAULT 5,
                  status TEXT NOT NULL DEFAULT 'queued',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL DEFAULT 3,
                  cycle INTEGER NOT NULL DEFAULT 0,
                  request_id TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  next_attempt_at REAL,
                  started_at REAL,
                  finished_at REAL,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  worker TEXT,
                  lease_expires_at REAL,
                  error TEXT,
                  retryable INTEGER,
                  result_ref TEXT,
                  dedupe_key TEXT,
                  meta TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_dedupe_key
                  ON tasks(dedupe_key) WHERE dedupe_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_tasks_ready
                  ON tasks(status, next_attempt_at, priority, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_request_id ON tasks(request_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_origin ON tasks(origin, status);

                CREATE TABLE IF NOT EXISTS task_events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                  at REAL NOT NULL,
                  kind TEXT NOT NULL,
                  message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, seq);

                CREATE TABLE IF NOT EXISTS queue_workers (
                  id TEXT PRIMARY KEY,
                  pid INTEGER,
                  boot_token TEXT,
                  status TEXT NOT NULL DEFAULT 'idle',
                  current_task TEXT,
                  started_at REAL NOT NULL,
                  last_seen REAL NOT NULL
                );
                """
            )
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _get_row(self, task_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def _event(self, task_id: str, kind: str, message: str | None = None, now: float | None = None) -> None:
        self._conn.execute(
            "INSERT INTO task_events (task_id, at, kind, message) VALUES (?,?,?,?)",
            (task_id, now or time.time(), kind, (message or "")[:4000] or None),
        )

    # ------------------------------------------------------------------- reads

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            return _row_to_task(self._get_row(task_id))

    def find_by_request_id(self, request_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE request_id=? AND status='awaiting_approval' "
                "ORDER BY updated_at DESC LIMIT 1", (request_id,)).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE request_id=? ORDER BY updated_at DESC LIMIT 1",
                    (request_id,)).fetchone()
            return _row_to_task(row)

    def list(self, status: str | None = None, limit: int = 50, offset: int = 0,
             origin: str | None = None) -> list[dict]:
        query = "SELECT * FROM tasks"
        clauses, params = [], []
        if status:
            if status not in ALL_STATUSES:
                raise QueueError(f"Unknown status: {status}")
            clauses.append("status=?")
            params.append(status)
        if origin:
            clauses.append("origin=?")
            params.append(origin)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with self._lock:
            return [_row_to_task(row) for row in self._conn.execute(query, params).fetchall()]

    def events(self, task_id: str, after_seq: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, at, kind, message FROM task_events WHERE task_id=? AND seq>? "
                "ORDER BY seq LIMIT ?", (task_id, int(after_seq), max(1, min(int(limit), 1000)))).fetchall()
        return [dict(row) for row in rows]

    def has_active_origin(self, origin: str) -> bool:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT 1 FROM tasks WHERE origin=? AND status IN ({marks}) LIMIT 1",
                (origin, *ACTIVE_STATUSES)).fetchone()
        return row is not None

    def workers(self, now: float | None = None, ttl_seconds: float = 120) -> list[dict]:
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM queue_workers ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["alive"] = (now - float(item["last_seen"])) <= ttl_seconds
            result.append(item)
        return result

    def stats(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            counts = {row["status"]: row["n"] for row in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()}
            ready = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM tasks "
                "WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                (now,)).fetchone()
            failed_24h = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status='failed' AND finished_at>?",
                (now - 86400,)).fetchone()["n"]
            retries = self._conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN attempts>1 THEN 1 ELSE 0 END), 0) AS n, "
                "COALESCE(SUM(attempts), 0) AS total FROM tasks").fetchone()
        all_counts = {status: counts.get(status, 0) for status in ALL_STATUSES}
        return {
            **all_counts,
            "total": sum(all_counts.values()),
            "queued_ready": ready["n"],
            "oldest_ready_seconds": round(now - float(ready["oldest"]), 1) if ready["oldest"] else None,
            "failed_last_24h": failed_24h,
            "tasks_with_retries": retries["n"],
            "attempts_total": retries["total"],
        }

    # ------------------------------------------------------------------ writes

    def enqueue(self, prompt: str, *, backend: str | None = None, skill_id: str | None = None,
                priority: int = 5, max_attempts: int = 3, dedupe_key: str | None = None,
                scheduled_for: float | None = None, origin: str | None = None,
                meta: dict | None = None, task_id: str | None = None,
                dedupe_window_seconds: float = 0, now: float | None = None) -> tuple[dict, bool]:
        """Insert a task. Returns (task, created). A duplicate (identical
        dedupe_key, or an identical prompt/backend/skill inside the dedupe
        window while still active) returns the existing task with created=False."""
        now = now or time.time()
        if not 1 <= int(priority) <= 9:
            raise QueueError("priority must be 1..9 (1 = highest)")
        if not 1 <= int(max_attempts) <= 10:
            raise QueueError("max_attempts must be 1..10")
        task_id = task_id or str(uuid.uuid4())
        with self._tx():
            if dedupe_key:
                existing = self._conn.execute("SELECT * FROM tasks WHERE dedupe_key=?", (dedupe_key,)).fetchone()
                if existing:
                    return _row_to_task(existing), False
            elif dedupe_window_seconds:
                existing = self._conn.execute(
                    "SELECT * FROM tasks WHERE prompt=? AND IFNULL(backend,'')=IFNULL(?,'') "
                    "AND IFNULL(skill_id,'')=IFNULL(?,'') AND created_at>? "
                    f"AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (prompt, backend, skill_id, now - float(dedupe_window_seconds), *ACTIVE_STATUSES)).fetchone()
                if existing:
                    return _row_to_task(existing), False
            self._conn.execute(
                "INSERT INTO tasks (id, origin, prompt, backend, skill_id, priority, status, attempts, "
                "max_attempts, cycle, request_id, created_at, updated_at, next_attempt_at, dedupe_key, meta) "
                "VALUES (?,?,?,?,?,?, 'queued', 0, ?, 0, NULL, ?, ?, ?, ?, ?)",
                (task_id, origin, prompt, backend, skill_id, int(priority), int(max_attempts),
                 now, now, scheduled_for, dedupe_key, json.dumps(meta or {}, ensure_ascii=False)))
            self._event(task_id, "enqueued",
                        f"origin={origin or 'api'} priority={priority} max_attempts={max_attempts}"
                        + (f" scheduled_for={scheduled_for}" if scheduled_for else ""), now=now)
        return self.get(task_id), True

    def claim(self, worker_id: str, *, lease_seconds: float = 960, now: float | None = None) -> dict | None:
        """Atomically claim the highest-priority ready task. First attempt uses
        the task id as its vault request id; retries append an attempt suffix."""
        now = now or time.time()
        with self._tx():
            row = self._conn.execute(
                "SELECT id FROM tasks WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "ORDER BY priority ASC, created_at ASC LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            task_id = row["id"]
            result = self._conn.execute(
                "UPDATE tasks SET status='running', worker=?, started_at=COALESCE(started_at, ?), "
                "lease_expires_at=?, attempts=attempts+1, next_attempt_at=NULL, updated_at=?, "
                "request_id=CASE "
                "  WHEN cycle=0 AND attempts=0 THEN id "
                "  WHEN cycle=0 THEN id || '-a' || (attempts+1) "
                "  ELSE id || '-r' || cycle || 'a' || (attempts+1) END "
                "WHERE id=? AND status='queued'",
                (worker_id, now, now + float(lease_seconds), now, task_id))
            if result.rowcount != 1:
                raise QueueError(f"claim lost race for {task_id}")
            task = self._get_row(task_id)
            self._event(task_id, "claimed",
                        f"worker={worker_id} attempt={task['attempts']}/{task['max_attempts']} "
                        f"request_id={task['request_id']}", now=now)
            return _row_to_task(task)

    def complete(self, task_id: str, *, result_ref: str | None = None, meta_patch: dict | None = None,
                 event: str = "completed", message: str | None = None, now: float | None = None) -> dict | None:
        return self._finalize(task_id, "completed", result_ref=result_ref, meta_patch=meta_patch,
                              event=event, message=message, now=now)

    def await_approval(self, task_id: str, *, meta_patch: dict | None = None, message: str | None = None,
                       now: float | None = None) -> dict | None:
        now = now or time.time()
        with self._tx():
            row = self._get_row(task_id)
            if row is None or row["status"] != "running":
                return _row_to_task(row)
            self._patch_meta(task_id, meta_patch)
            self._conn.execute(
                "UPDATE tasks SET status='awaiting_approval', worker=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE id=?", (now, task_id))
            self._event(task_id, "awaiting_approval", message, now=now)
        return self.get(task_id)

    def fail(self, task_id: str, error: str, *, retryable: bool, backoff_base: float = 30,
             backoff_cap: float = 900, now: float | None = None) -> dict | None:
        """Record a failed attempt. Retryable failures with attempts left are
        re-queued with exponential backoff + jitter; otherwise the task is
        terminally failed."""
        now = now or time.time()
        error = (error or "unknown error")[:2000]
        with self._tx():
            row = self._get_row(task_id)
            if row is None or row["status"] != "running":
                return _row_to_task(row)
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if retryable and attempts < max_attempts:
                delay = min(float(backoff_cap), float(backoff_base) * (2 ** (attempts - 1)))
                delay = max(1.0, delay * random.uniform(0.8, 1.2))
                self._conn.execute(
                    "UPDATE tasks SET status='queued', next_attempt_at=?, worker=NULL, lease_expires_at=NULL, "
                    "error=?, retryable=1, updated_at=? WHERE id=?",
                    (now + delay, error, now, task_id))
                self._event(task_id, "retry_scheduled",
                            f"attempt {attempts}/{max_attempts} failed; retrying in {delay:.0f}s: {error[:300]}",
                            now=now)
            else:
                self._conn.execute(
                    "UPDATE tasks SET status='failed', finished_at=?, worker=NULL, lease_expires_at=NULL, "
                    "error=?, retryable=?, updated_at=? WHERE id=?",
                    (now, error, int(bool(retryable)), now, task_id))
                self._event(task_id, "failed",
                            f"attempt {attempts}/{max_attempts}: {error[:500]}", now=now)
        return self.get(task_id)

    def cancel_queued(self, task_id: str, *, reason: str = "cancelled before execution",
                      now: float | None = None) -> dict | None:
        now = now or time.time()
        with self._tx():
            row = self._get_row(task_id)
            if row is None or row["status"] not in ("queued", "awaiting_approval"):
                return _row_to_task(row)
            self._conn.execute(
                "UPDATE tasks SET status='cancelled', finished_at=?, worker=NULL, lease_expires_at=NULL, "
                "error=COALESCE(?, error), updated_at=? WHERE id=?",
                (now, reason, now, task_id))
            self._event(task_id, "cancelled", reason, now=now)
        return self.get(task_id)

    def request_cancel(self, task_id: str, *, now: float | None = None) -> bool:
        """Cooperative cancellation: flag a running task so the owning worker cancels it."""
        now = now or time.time()
        with self._tx():
            row = self._get_row(task_id)
            if row is None or row["status"] != "running":
                return False
            self._conn.execute(
                "UPDATE tasks SET cancel_requested=1, updated_at=? WHERE id=? AND status='running'",
                (now, task_id))
            self._event(task_id, "cancel_requested", "cooperative cancel requested", now=now)
            return True

    def mark_cancelled(self, task_id: str, *, reason: str = "cancelled while running",
                       now: float | None = None) -> dict | None:
        now = now or time.time()
        with self._tx():
            row = self._get_row(task_id)
            if row is None or row["status"] not in ("running", "queued"):
                return _row_to_task(row)
            self._conn.execute(
                "UPDATE tasks SET status='cancelled', finished_at=?, worker=NULL, lease_expires_at=NULL, "
                "error=COALESCE(?, error), updated_at=? WHERE id=?",
                (now, reason, now, task_id))
            self._event(task_id, "cancelled", reason, now=now)
        return self.get(task_id)

    def requeue(self, task_id: str, *, delay_seconds: float, event: str = "requeued",
                message: str | None = None, now: float | None = None) -> dict | None:
        """Return a running task to the queue (restart interruption, reconciliation)."""
        now = now or time.time()
        with self._tx():
            row = self._get_row(task_id)
            if row is None or row["status"] != "running":
                return _row_to_task(row)
            self._conn.execute(
                "UPDATE tasks SET status='queued', next_attempt_at=?, worker=NULL, lease_expires_at=NULL, "
                "cancel_requested=0, updated_at=? WHERE id=?",
                (now + max(1.0, float(delay_seconds)), now, task_id))
            self._event(task_id, event, message, now=now)
        return self.get(task_id)

    def retry(self, task_id: str, *, now: float | None = None) -> tuple[dict | None, bool]:
        """Manual retry of a failed/cancelled task: new cycle, fresh attempts."""
        now = now or time.time()
        with self._tx():
            row = self._get_row(task_id)
            if row is None:
                return None, False
            if row["status"] not in TERMINAL_STATUSES:
                return _row_to_task(row), False
            self._conn.execute(
                "UPDATE tasks SET status='queued', attempts=0, cycle=cycle+1, error=NULL, retryable=NULL, "
                "finished_at=NULL, started_at=NULL, next_attempt_at=?, worker=NULL, lease_expires_at=NULL, "
                "cancel_requested=0, updated_at=? WHERE id=?",
                (now, now, task_id))
            self._event(task_id, "manual_retry", f"cycle={int(row['cycle']) + 1}", now=now)
        return self.get(task_id), True

    def recover(self, *, boot_token: str | None = None, now: float | None = None) -> dict:
        """Startup recovery: tasks left running by a dead process go back to the
        queue (attempt already counted) or fail when attempts are exhausted.
        Stale worker rows and rows from previous boots are dropped."""
        now = now or time.time()
        recovered = failed = 0
        with self._tx():
            rows = self._conn.execute("SELECT * FROM tasks WHERE status='running'").fetchall()
            for row in rows:
                if int(row["attempts"]) >= int(row["max_attempts"]):
                    self._conn.execute(
                        "UPDATE tasks SET status='failed', finished_at=?, worker=NULL, lease_expires_at=NULL, "
                        "error=?, updated_at=? WHERE id=?",
                        (now, "interrupted by restart; attempts exhausted", now, row["id"]))
                    self._event(row["id"], "recovered_failed",
                                "interrupted by restart; attempts exhausted", now=now)
                    failed += 1
                else:
                    delay = random.uniform(2, 8)
                    self._conn.execute(
                        "UPDATE tasks SET status='queued', next_attempt_at=?, worker=NULL, lease_expires_at=NULL, "
                        "updated_at=? WHERE id=?",
                        (now + delay, now, row["id"]))
                    self._event(row["id"], "recovered_after_restart",
                                f"attempt {int(row['attempts'])}/{int(row['max_attempts'])} interrupted; "
                                f"requeued in {delay:.0f}s", now=now)
                    recovered += 1
            removed = self._conn.execute(
                "DELETE FROM queue_workers WHERE last_seen<?", (now - 120,)).rowcount
            if boot_token is not None:
                removed += self._conn.execute(
                    "DELETE FROM queue_workers WHERE boot_token IS NULL OR boot_token<>?",
                    (boot_token,)).rowcount
        if recovered or failed or removed:
            logger.info("queue recovery: %d requeued, %d failed, %d stale workers removed",
                        recovered, failed, removed)
        return {"requeued": recovered, "failed": failed, "workers_removed": removed}

    def worker_heartbeat(self, worker_id: str, *, status: str = "idle", current_task: str | None = None,
                         pid: int | None = None, boot_token: str | None = None,
                         now: float | None = None) -> None:
        now = now or time.time()
        with self._tx():
            row = self._conn.execute("SELECT id FROM queue_workers WHERE id=?", (worker_id,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO queue_workers (id, pid, boot_token, status, current_task, started_at, last_seen) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (worker_id, pid, boot_token, status, current_task, now, now))
            else:
                self._conn.execute(
                    "UPDATE queue_workers SET status=?, current_task=?, last_seen=? WHERE id=?",
                    (status, current_task, now, worker_id))

    def prune(self, retention_days: float, *, now: float | None = None) -> int:
        now = now or time.time()
        cutoff = now - float(retention_days) * 86400
        with self._tx():
            removed = self._conn.execute(
                f"DELETE FROM tasks WHERE status IN ({','.join('?' for _ in TERMINAL_STATUSES)}) "
                "AND finished_at IS NOT NULL AND finished_at<?", (*TERMINAL_STATUSES, cutoff)).rowcount
            if removed:
                self._conn.execute(
                    f"DELETE FROM task_events WHERE task_id NOT IN (SELECT id FROM tasks)").rowcount
        return removed

    # ----------------------------------------------------------------- helpers

    def _patch_meta(self, task_id: str, meta_patch: dict | None) -> None:
        if not meta_patch:
            return
        row = self._get_row(task_id)
        if row is None:
            return
        try:
            meta = json.loads(row["meta"] or "{}")
        except ValueError:
            meta = {}
        meta.update(meta_patch)
        self._conn.execute("UPDATE tasks SET meta=? WHERE id=?",
                           (json.dumps(meta, ensure_ascii=False), task_id))

    def _finalize(self, task_id: str, status: str, *, result_ref: str | None = None,
                  meta_patch: dict | None = None, event: str | None = None,
                  message: str | None = None, error: str | None = None,
                  now: float | None = None) -> dict | None:
        now = now or time.time()
        with self._tx():
            row = self._get_row(task_id)
            if row is None:
                return None
            self._patch_meta(task_id, meta_patch)
            self._conn.execute(
                "UPDATE tasks SET status=?, finished_at=COALESCE(finished_at, ?), worker=NULL, "
                "lease_expires_at=NULL, result_ref=COALESCE(?, result_ref), error=COALESCE(?, error), "
                "updated_at=? WHERE id=?",
                (status, now, result_ref, error, now, task_id))
            if event:
                self._event(task_id, event, message, now=now)
        return self.get(task_id)


class QueueManager:
    """In-process worker pool for the durable queue.

    ``event_sink``, when provided, receives ``(task_id, event, finish)`` where
    event is a stream event (``output`` / ``task``) or a final result dict
    when ``finish`` is true. The API layer bridges this to request streams so
    the HUD keeps using one streaming mechanism.
    """

    def __init__(self, service, store: QueueStore, *, workers: int = 2, poll_interval: float = 0.5,
                 task_timeout: float = 900, backoff_base: float = 30, backoff_cap: float = 900,
                 max_attempts: int = 3, retention_days: float = 30,
                 event_sink: Callable[[str, dict, bool], Awaitable[None]] | None = None):
        self.service = service
        self.store = store
        self.workers = max(1, int(workers))
        self.poll_interval = max(0.05, float(poll_interval))
        self.task_timeout = float(task_timeout)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.max_attempts = int(max_attempts)
        self.retention_days = float(retention_days)
        self.event_sink = event_sink
        self._executions: dict[str, asyncio.Task] = {}
        self._worker_loops: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._boot_token = uuid.uuid4().hex[:8]
        self.recovery_report: dict = {}

    # ----------------------------------------------------------------- control

    async def start(self) -> None:
        self.recovery_report = self.store.recover(boot_token=self._boot_token)
        reconciled = self.reconcile_awaiting()
        pruned = self.store.prune(self.retention_days)
        for index in range(self.workers):
            worker_id = f"w{index + 1}-{self._boot_token}"
            self._worker_loops.append(asyncio.create_task(self._worker_loop(worker_id), name=f"queue-{worker_id}"))
        logger.info("queue started: %d worker(s), recovery=%s, reconciled=%d, pruned=%d",
                    self.workers, self.recovery_report, reconciled, pruned)

    async def stop(self) -> None:
        self._stop.set()
        all_tasks = [*self._worker_loops, *self._executions.values()]
        for task in all_tasks:
            task.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._worker_loops.clear()
        self._executions.clear()
        self.store.close()

    def reconcile_awaiting(self) -> int:
        """Resolve tasks that were waiting for approval when the process died.

        A task still present in the live pending map stays as-is; otherwise the
        vault's request/output records decide: completed output -> completed,
        rejected -> cancelled, no decision found -> failed (manual retry works).
        """
        resolved = 0
        pending = getattr(self.service, "pending", {})
        for task in self.store.list(status="awaiting_approval", limit=1000):
            request_id = task.get("request_id")
            if not request_id or request_id in pending:
                continue
            outcome = self._vault_outcome(request_id)
            if outcome and outcome[0] == "completed":
                self.store.complete(task["id"], result_ref=outcome[1],
                                    event="reconciled_completed", message="output found in vault after restart")
            elif outcome and outcome[0] == "rejected":
                self.store.cancel_queued(task["id"], reason="approval was rejected")
            else:
                reason = "approval decision missing after restart (expired or cleared)"
                self.store._finalize(task["id"], "failed", event="reconciled_failed",
                                     message=reason, error=reason)
            resolved += 1
        return resolved

    # ------------------------------------------------------------------- api

    async def enqueue(self, prompt: str, *, backend: str | None = None, skill_id: str | None = None,
                      priority: int = 5, max_attempts: int | None = None,
                      dedupe_key: str | None = None, scheduled_for: float | None = None,
                      origin: str | None = None, meta: dict | None = None,
                      task_id: str | None = None, dedupe_window: float = 0.0) -> dict:
        task, created = self.store.enqueue(
            prompt, backend=backend, skill_id=skill_id, priority=priority,
            max_attempts=max_attempts or self.max_attempts, dedupe_key=dedupe_key,
            scheduled_for=scheduled_for, origin=origin, meta=meta, task_id=task_id,
            dedupe_window_seconds=dedupe_window)
        if created and task:
            await self._emit(task["id"], {"type": "task", "status": "queued", "task": _summary(task)})
        return {"task": task, "created": created}

    async def cancel(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if task is None:
            raise QueueError("Task not found")
        if task["status"] == "running":
            if not self.store.request_cancel(task_id):
                return {"id": task_id, "status": task["status"], "detail": "task is no longer running"}
            execution = self._executions.get(task_id)
            if execution is not None:
                execution.cancel()
            return {"id": task_id, "status": "cancel_requested"}
        if task["status"] in ("queued", "awaiting_approval"):
            if task["status"] == "awaiting_approval" and task.get("request_id"):
                with contextlib.suppress(Exception):
                    self.service.reject(task["request_id"])
            cancelled = self.store.cancel_queued(task_id, reason="cancelled by human")
            await self._emit(task_id, {"type": "task", "status": "cancelled", "task": _summary(cancelled)})
            return {"id": task_id, "status": "cancelled"}
        return {"id": task_id, "status": task["status"], "detail": "task already finished"}

    async def retry(self, task_id: str) -> dict:
        task, retried = self.store.retry(task_id)
        if task is None:
            raise QueueError("Task not found")
        if not retried:
            return {"id": task_id, "status": task["status"], "detail": "only failed or cancelled tasks can be retried"}
        await self._emit(task_id, {"type": "task", "status": "queued", "task": _summary(task)})
        return {"id": task_id, "status": "queued", "task": _summary(task)}

    def request_cancel_by_request_id(self, request_id: str) -> bool:
        """Sync helper for interrupt paths (WebSocket stop button): cooperatively
        cancel the running task that owns this request id or task id. Returns
        True when a stop was actually sent."""
        task = self.store.find_by_request_id(request_id) or self.store.get(request_id)
        if task is None or task["status"] != "running":
            return False
        self.store.request_cancel(task["id"])
        execution = self._executions.get(task["id"])
        if execution is not None:
            execution.cancel()
        return True

    async def note_approval_outcome(self, request_id: str, result: dict) -> None:
        task = self.store.find_by_request_id(request_id)
        if task is None or task["id"] == "" or task["status"] != "awaiting_approval":
            return
        status = result.get("status")
        if status == "completed":
            self.store.complete(task["id"], result_ref=result.get("output"),
                                meta_patch={"tier": result.get("tier"), "backend": result.get("backend"),
                                            "receipt": result.get("receipt")},
                                event="completed_after_approval")
            await self._finish_stream(task["id"], result)
        elif status == "error":
            updated = self.store.fail(task["id"], str(result.get("error", "error after approval")),
                                      retryable=False)
            await self._finish_stream(task["id"],
                                      {"id": task["id"], "status": "error",
                                       "error": str(result.get("error", "")), "task": task["id"]})
        else:
            updated = self.store.cancel_queued(task["id"], reason=f"approval ended with status {status}")

    async def note_rejection(self, request_id: str) -> None:
        task = self.store.find_by_request_id(request_id)
        if task is None or task["status"] != "awaiting_approval":
            return
        self.store.cancel_queued(task["id"], reason="approval rejected by human")
        await self._finish_stream(task["id"],
                                  {"id": task["id"], "status": "rejected", "task": task["id"]})

    # ---------------------------------------------------------------- workers

    async def run_pending_once(self) -> dict:
        """Execute at most one claimed task; used by tests and manual drains."""
        task = self.store.claim(f"manual-{self._boot_token}", lease_seconds=self.task_timeout + 60)
        if task is None:
            return {"claimed": False}
        await self._run_task("manual", task)
        return {"claimed": True, "task": self.store.get(task["id"])}

    async def _worker_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            try:
                self.store.worker_heartbeat(worker_id, status="idle", pid=os.getpid(),
                                            boot_token=self._boot_token)
                task = self.store.claim(worker_id, lease_seconds=self.task_timeout + 60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("queue worker %s failed to claim; backing off", worker_id)
                await _sleep_or_stop(self._stop, 2.0)
                continue
            if task is None:
                await _sleep_or_stop(self._stop, self.poll_interval)
                continue
            try:
                await self._run_task(worker_id, task)
            except asyncio.CancelledError:
                # stop() cancelled us; _run_task already recorded the outcome.
                raise
            except Exception:
                logger.exception("queue worker %s crashed running %s", worker_id, task["id"])
                with contextlib.suppress(Exception):
                    self.store.fail(task["id"], "worker crashed; see bridge logs", retryable=True,
                                    backoff_base=self.backoff_base, backoff_cap=self.backoff_cap)

    async def _run_task(self, worker_id: str, task: dict) -> None:
        task_id = task["id"]
        request_id = task.get("request_id") or task_id
        self.store.worker_heartbeat(worker_id, status="running", current_task=task_id)
        await self._emit(task_id, {"type": "task", "status": "running", "task": _summary(task)})

        async def on_output(text: str) -> None:
            await self._emit(task_id, {"type": "output", "data": text})

        execution = asyncio.current_task()
        assert execution is not None
        self._executions[task_id] = execution
        result: dict | None = None
        try:
            result = await asyncio.wait_for(
                self.service.submit(task["prompt"], task.get("backend"), task.get("skill_id"),
                                    request_id=request_id, on_output=on_output),
                timeout=self.task_timeout)
        except asyncio.CancelledError:
            if self._stop.is_set():
                updated = self.store.requeue(task_id, delay_seconds=random.uniform(2, 8),
                                             event="interrupted_by_shutdown",
                                             message="service is restarting; attempt will be retried")
                logger.info("queue task %s interrupted by shutdown -> %s", task_id, updated["status"])
            else:
                updated = self.store.mark_cancelled(task_id, reason="cancelled by human while running")
                await self._emit(task_id, {"type": "task", "status": "cancelled", "task": _summary(updated)})
                await self._finish_stream(task_id, {"id": task_id, "status": "cancelled",
                                                    "task": task_id})
            return
        except asyncio.TimeoutError:
            await self._fail_or_cancel(task_id, request_id,
                                       f"task exceeded queue timeout ({self.task_timeout:.0f}s)",
                                       retryable=True)
            return
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", True))
            await self._fail_or_cancel(task_id, request_id, str(exc), retryable=retryable)
            return
        finally:
            self._executions.pop(task_id, None)
            with contextlib.suppress(Exception):
                self.store.worker_heartbeat(worker_id, status="idle")

        status = (result or {}).get("status")
        if status == "completed":
            updated = self.store.complete(
                task_id, result_ref=result.get("output"),
                meta_patch={"tier": result.get("tier"), "backend": result.get("backend"),
                            "skill": result.get("skill"), "route_reason": result.get("route_reason"),
                            "receipt": result.get("receipt")},
                event="completed")
            await self._emit(task_id, {"type": "task", "status": "completed", "task": _summary(updated)})
            await self._finish_stream(task_id, result)
        elif status == "awaiting_approval":
            updated = self.store.await_approval(
                task_id, meta_patch={"risk_level": result.get("risk_level"), "reason": result.get("reason")},
                message=f"risk={result.get('risk_level')}: {result.get('reason')}")
            await self._emit(task_id, {"type": "task", "status": "awaiting_approval", "task": _summary(updated)})
            await self._finish_stream(task_id, result)
        elif status == "error":
            error = str(result.get("error", "unknown error"))
            if "already been used" in error:
                await self._handle_duplicate(task, error)
            else:
                await self._fail_or_cancel(task_id, request_id, error,
                                           retryable=bool(result.get("retryable", False)))
        else:
            await self._fail_or_cancel(task_id, request_id, f"unexpected submit status: {status!r}",
                                       retryable=False)

    async def _fail_or_cancel(self, task_id: str, request_id: str | None, error: str,
                              *, retryable: bool) -> None:
        """Record the failed attempt. If the human already asked to stop this
        task, record a cancellation instead of a retryable failure so the
        provider's post-interrupt error output cannot silently re-run."""
        row = self.store.get(task_id)
        if row and row.get("cancel_requested"):
            updated = self.store.mark_cancelled(task_id, reason="cancelled by human while running")
            await self._emit(task_id, {"type": "task", "status": "cancelled", "task": _summary(updated)})
            await self._finish_stream(task_id, {"id": task_id, "status": "cancelled",
                                                "task": task_id, "error": error})
            return
        updated = self.store.fail(task_id, error, retryable=retryable,
                                  backoff_base=self.backoff_base, backoff_cap=self.backoff_cap)
        await self._after_attempt(updated, task_id)

    async def _handle_duplicate(self, task: dict, error: str) -> None:
        """The attempt's request id already exists in the vault (crash between
        the request log and the finalize step). Treat a completed vault record
        as success; otherwise fail without retrying."""
        task_id = task["id"]
        outcome = self._vault_outcome(task.get("request_id") or task_id)
        if outcome and outcome[0] == "completed":
            updated = self.store.complete(task_id, result_ref=outcome[1],
                                          meta_patch={"recovered_duplicate": True},
                                          event="completed_after_recovery",
                                          message="request id already used; completed vault record found")
            await self._emit(task_id, {"type": "task", "status": "completed", "task": _summary(updated)})
            await self._finish_stream(task_id, {"id": task_id, "status": "completed", "task": task_id})
        else:
            updated = self.store.fail(task_id, f"duplicate request id without completed record: {error}",
                                      retryable=False, backoff_base=self.backoff_base,
                                      backoff_cap=self.backoff_cap)
            await self._after_attempt(updated, task_id)

    async def _after_attempt(self, updated: dict | None, task_id: str) -> None:
        if updated is None:
            return
        if updated["status"] == "queued":
            await self._emit(task_id, {"type": "task", "status": "queued",
                                       "attempts": updated["attempts"],
                                       "next_attempt_at": updated.get("next_attempt_at"),
                                       "task": _summary(updated)})
            await self._finish_stream(task_id, {"id": task_id, "status": "retrying",
                                                "error": updated.get("error"),
                                                "attempts": updated["attempts"],
                                                "next_attempt_at": updated.get("next_attempt_at"),
                                                "task": task_id})
        else:
            await self._emit(task_id, {"type": "task", "status": updated["status"], "task": _summary(updated)})
            await self._finish_stream(task_id, {"id": task_id, "status": "error",
                                                "error": updated.get("error"), "task": task_id})

    # ------------------------------------------------------------------ vault

    def _vault_outcome(self, request_id: str) -> tuple[str, str] | None:
        vault = getattr(self.service, "vault", None)
        root = getattr(vault, "root", None)
        if root is None:
            return None
        try:
            safe = safe_name(request_id)
            outputs = sorted((root / "output").glob(f"**/*-{safe}.md"))
            if outputs:
                return ("completed", str(outputs[-1]))
            request_file = root / "requests" / f"{safe}.md"
            if request_file.exists():
                last = None
                for line in request_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = re.search(r"Status: `([a-z_]+)`", line)
                    if match:
                        last = match.group(1)
                if last == "completed":
                    return ("completed", str(request_file))
                if last == "rejected":
                    return ("rejected", str(request_file))
            return None
        except Exception:
            logger.exception("vault outcome lookup failed for %s", request_id)
            return None

    # ----------------------------------------------------------------- streams

    async def _emit(self, task_id: str, event: dict) -> None:
        if not self.event_sink:
            return
        try:
            await self.event_sink(task_id, event, False)
        except Exception:
            logger.exception("queue event sink failed for %s", task_id)

    async def _finish_stream(self, request_id: str | None, result: dict) -> None:
        if not self.event_sink or not request_id:
            return
        try:
            await self.event_sink(request_id, result, True)
        except Exception:
            logger.exception("queue stream finish failed for %s", request_id)


def _summary(task: dict | None) -> dict | None:
    if task is None:
        return None
    keys = ("id", "status", "origin", "priority", "attempts", "max_attempts", "cycle",
            "created_at", "updated_at", "next_attempt_at", "started_at", "finished_at",
            "error", "result_ref", "request_id", "skill_id", "backend", "meta")
    return {key: task.get(key) for key in keys}


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
