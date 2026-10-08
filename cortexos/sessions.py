from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios
import uuid
from collections import deque
from dataclasses import dataclass, field

from .config import Settings


@dataclass
class TerminalSession:
    id: str
    backend: str
    process: asyncio.subprocess.Process
    master_fd: int
    cols: int
    rows: int
    history: deque[tuple[int, str]] = field(default_factory=lambda: deque(maxlen=4000))
    sequence: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    eof: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int | None = None

    async def append(self, text: str) -> None:
        async with self.condition:
            self.sequence += 1
            self.history.append((self.sequence, text))
            self.condition.notify_all()

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(20, min(cols, 300))
        self.rows = max(5, min(rows, 120))
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))

    def write(self, value: str) -> None:
        if self.exit_code is not None:
            return
        os.write(self.master_fd, value.encode("utf-8", errors="replace"))

    def interrupt(self) -> None:
        if self.exit_code is None:
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass


class TerminalManager:
    """PTY-backed, interactive Claude Code and Codex sessions for a local Bridge."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, TerminalSession] = {}
        self.reader_tasks: set[asyncio.Task] = set()

    async def spawn(self, backend: str, cols: int = 100, rows: int = 28) -> TerminalSession:
        if backend not in {"claude", "codex"}:
            raise ValueError("backend must be 'claude' or 'codex'")
        if len(self.sessions) >= 12:
            ended = [key for key, item in self.sessions.items() if item.exit_code is not None]
            for key in ended:
                self._forget(key)
            if len(self.sessions) >= 12:
                raise RuntimeError("At most 12 terminal sessions can be open at once")
        config = (self.settings.providers or {}).get(backend, {})
        command = config.get("terminal_command", config.get("command", backend))
        args = config.get("terminal_args", [])
        master_fd, slave_fd = pty.openpty()
        cols, rows = max(20, min(cols, 300)), max(5, min(rows, 120))
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = os.environ.copy()
        env.update({"TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)})
        try:
            process = await asyncio.create_subprocess_exec(
                command, *args,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                cwd=str(self.settings.vault), env=env, start_new_session=True,
                pass_fds=(slave_fd,), preexec_fn=lambda: fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0),
            )
        except (FileNotFoundError, PermissionError):
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        session = TerminalSession(str(uuid.uuid4()), backend, process, master_fd, cols, rows)
        self.sessions[session.id] = session
        loop = asyncio.get_running_loop()

        def on_readable() -> None:
            try:
                chunk = os.read(master_fd, 8192)
            except BlockingIOError:
                return
            except OSError:
                loop.remove_reader(master_fd)
                session.eof.set()
                return
            if chunk:
                task = asyncio.create_task(session.append(chunk.decode("utf-8", errors="replace")))
                self.reader_tasks.add(task)
                task.add_done_callback(self.reader_tasks.discard)
            else:
                loop.remove_reader(master_fd)
                session.eof.set()

        os.set_blocking(master_fd, False)
        loop.add_reader(master_fd, on_readable)
        task = asyncio.create_task(self._watch_process(session, loop))
        self.reader_tasks.add(task)
        task.add_done_callback(self.reader_tasks.discard)
        return session

    async def _watch_process(self, session: TerminalSession, loop: asyncio.AbstractEventLoop) -> None:
        exit_code = await session.process.wait()
        try:
            await asyncio.wait_for(session.eof.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            loop.remove_reader(session.master_fd)
            session.eof.set()
        session.exit_code = exit_code
        await session.append(f"\r\n\x1b[90m[process exited with code {exit_code}]\x1b[0m\r\n")

    def get(self, session_id: str) -> TerminalSession | None:
        return self.sessions.get(session_id)

    async def terminate(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        if session.exit_code is None:
            session.interrupt()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(session.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(session.process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(session.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await session.process.wait()
        self._forget(session_id)
        return True

    def _forget(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session:
            try:
                asyncio.get_running_loop().remove_reader(session.master_fd)
            except RuntimeError:
                pass
            try:
                os.close(session.master_fd)
            except OSError:
                pass

    async def close_all(self) -> None:
        for session in list(self.sessions.values()):
            await self.terminate(session.id)


async def terminal_websocket(websocket, session: TerminalSession) -> None:
    await websocket.accept()
    cursor = 0

    async def output_loop() -> None:
        nonlocal cursor
        while True:
            async with session.condition:
                await session.condition.wait_for(lambda: session.sequence > cursor or session.exit_code is not None)
                pending = [(seq, chunk) for seq, chunk in session.history if seq > cursor]
                exited = session.exit_code is not None
            for seq, chunk in pending:
                await websocket.send_json({"type": "output", "sequence": seq, "data": chunk})
                cursor = seq
            if exited and cursor >= session.sequence:
                await websocket.send_json({"type": "exit", "code": session.exit_code})
                await websocket.close()
                return

    async def input_loop() -> None:
        while True:
            message = json.loads(await websocket.receive_text())
            kind = message.get("type")
            if kind == "input":
                session.write(str(message.get("data", "")))
            elif kind == "resize":
                session.resize(int(message.get("cols", 100)), int(message.get("rows", 28)))
            elif kind == "interrupt":
                session.interrupt()

    import contextlib
    tasks = [asyncio.create_task(output_loop()), asyncio.create_task(input_loop())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
