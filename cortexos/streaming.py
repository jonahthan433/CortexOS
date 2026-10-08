from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RequestStream:
    request_id: str
    events: deque[tuple[int, dict]] = field(default_factory=lambda: deque(maxlen=7000))
    sequence: int = 0
    done: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def publish(self, event: dict) -> None:
        async with self.condition:
            self.sequence += 1
            self.events.append((self.sequence, event))
            self.condition.notify_all()

    async def finish(self, result: dict) -> None:
        response = dict(result)
        if isinstance(response.get("result"), str):
            response["result"] = response["result"][:8000]
        await self.publish({"type": "done", "result": response})
        async with self.condition:
            self.done = True
            self.condition.notify_all()


class RequestStreams:
    def __init__(self):
        self.streams: dict[str, RequestStream] = {}

    def create(self, request_id: str) -> RequestStream:
        stream = RequestStream(request_id)
        self.streams[request_id] = stream
        return stream

    def get(self, request_id: str) -> RequestStream | None:
        return self.streams.get(request_id)

    async def serve(self, websocket, stream: RequestStream, interrupt) -> None:
        await websocket.accept()
        cursor = 0

        async def output_loop() -> None:
            nonlocal cursor
            while True:
                async with stream.condition:
                    await stream.condition.wait_for(lambda: stream.sequence > cursor or stream.done)
                    pending = [(seq, event) for seq, event in stream.events if seq > cursor]
                    done = stream.done
                for sequence, event in pending:
                    await websocket.send_json(event)
                    cursor = sequence
                if done and cursor >= stream.sequence:
                    await websocket.close()
                    return

        async def input_loop() -> None:
            while True:
                message = json.loads(await websocket.receive_text())
                if message.get("type") == "interrupt":
                    stopped = interrupt(stream.request_id)
                    await stream.publish({"type": "notice", "text": "Interrupt signal sent." if stopped else "No active agent process for this request."})

        tasks = [asyncio.create_task(output_loop()), asyncio.create_task(input_loop())]
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in tasks:
                if task.done():
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
