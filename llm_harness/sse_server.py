"""SSE server: GET /events (docs/API.md). Ring-buffer catchup via ?since=<seq>.

Also serves app updates over the same port (no extra port, no S3):
  GET /update/manifest   -> {"version": N, "filename": "app-N.apk", ...}
  GET /update/<file>.apk -> the APK file itself
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)

from .events import EventBus
from .harness import Harness
from .update import DEFAULT_RELEASES_DIR, manifest, resolve

HEARTBEAT_S = 15.0


def create_app(harness: Harness,
               releases_dir: Path = DEFAULT_RELEASES_DIR) -> FastAPI:
    app = FastAPI(title="llm-harness SSE")
    bus: EventBus = harness.events
    releases = Path(releases_dir)

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

    @app.get("/update")
    async def update_index():
        """Bare /update -> 302 to the latest release (public, no token)."""
        fn = manifest(releases).get("filename")
        if fn and (releases / fn).is_file():
            return RedirectResponse(f"/update/{fn}", status_code=302)
        return JSONResponse({"error": "no release yet"}, status_code=404)

    @app.get("/update/manifest")
    async def update_manifest():
        return JSONResponse(manifest(releases),
                            headers={"Cache-Control": "no-store"})

    @app.get("/update/{filename}")
    async def update_file(filename: str):
        p = resolve(releases, filename)
        if p is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            p, media_type="application/vnd.android.package-archive",
            filename=p.name,
            headers={"Cache-Control": "no-store",
                     "X-Accel-Buffering": "no"})

    return app