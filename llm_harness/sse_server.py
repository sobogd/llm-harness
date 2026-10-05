"""SSE server: GET /events (docs/API.md). Ring-buffer catchup via ?since=<seq>."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .events import EventBus
from .harness import Harness

HEARTBEAT_S = 15.0


def create_app(harness: Harness) -> FastAPI:
    app = FastAPI(title="llm-harness SSE")

    bus: EventBus = harness.events

    @app.get("/health")
    async def health():
        return {"ok": True, "state": harness.state, "seq": bus.last_seq}

    @app.get("/events")
    async def events(request: Request, since: int = Query(0)):
        q = bus.subscribe()

        async def gen():
            try:
                for ev in bus.events_since(since):
                    if await request.is_disconnected():
                        return
                    yield EventBus.sse_format(ev)
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield EventBus.sse_format(ev)
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"})

    return app