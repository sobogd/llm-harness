from __future__ import annotations

import asyncio
import hmac
import json
import os
import signal
import sys
from collections import deque

import aiohttp
import grpc
from aiohttp import web
from aiohttp.client_exceptions import ClientError
from google.protobuf import json_format

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_harness import llm_harness_pb2 as pb
from llm_harness import llm_harness_pb2_grpc as pb_grpc

DEFAULT_CONFIG = os.path.expanduser("~/.llm-bridge.json")
RING_FRAMES = 1024


class Bridge:
    def __init__(self, cfg: dict):
        self.token = cfg["token"]
        self.grpc_target = cfg.get("grpc", "127.0.0.1:19000")
        self.sse_url = cfg.get("sse", "http://127.0.0.1:19001/events")
        self.ring: deque[bytes] = deque(maxlen=RING_FRAMES)
        self.subs: set[asyncio.Queue] = set()
        self.channel = grpc.aio.insecure_channel(self.grpc_target)
        self.stub = pb_grpc.HarnessStub(self.channel)

    def authorized(self, request: web.Request) -> bool:
        h = request.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            tok = h[7:]
        else:
            tok = request.rel_url.query.get("token", "")
        return hmac.compare_digest(tok, self.token)

    @staticmethod
    def _since(request: web.Request) -> int:
        try:
            return int(request.rel_url.query.get("since", "0") or 0)
        except ValueError:
            return 0

    @staticmethod
    def _frame_seq(frame: bytes) -> int | None:
        first = frame.partition(b"\n")[0]
        if first.startswith(b"id:"):
            try:
                return int(first[3:].strip())
            except ValueError:
                return None
        return None

    async def _call(self, request, fn):
        try:
            reply = await fn()
        except grpc.aio.AioRpcError as e:
            return web.Response(status=502, text=f"upstream {e.code().name}")
        return web.json_response(
            json_format.MessageToDict(reply,
                                      preserving_proto_field_name=True))

    async def ask(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        body = await request.json()
        req = pb.AskRequest(prompt=body.get("prompt", ""),
                            system_note=body.get("system_note", ""),
                            session_id=body.get("session_id", ""))
        return await self._call(request, lambda: self.stub.Ask(req))

    async def stop(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        return await self._call(request, lambda: self.stub.Stop(pb.Empty()))

    async def resume(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        return await self._call(request, lambda: self.stub.Resume(pb.Empty()))

    async def status(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        return await self._call(request, lambda: self.stub.Status(pb.Empty()))

    async def get_messages(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        text = await request.text()
        body = json.loads(text) if text else {}
        req = pb.GetMessagesRequest(last=int(body.get("last", 0)))
        return await self._call(request, lambda: self.stub.GetMessages(req))

    async def settings(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        body = await request.json()
        req = pb.Settings(
            max_context_tokens=int(body.get("max_context_tokens", 0) or 0),
            max_output_tokens=int(body.get("max_output_tokens", 0) or 0),
            thinking_enabled=bool(body.get("thinking_enabled", False))
            if body.get("thinking_enabled") is not None else False,
            thinking_effort=str(body.get("thinking_effort", "") or ""))
        return await self._call(request, lambda: self.stub.SetSettings(req))

    async def compact(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        body = await request.json()
        req = pb.CompactRequest(keep_last_messages=int(body.get("keep_last_messages", 0) or 0))
        return await self._call(request, lambda: self.stub.Compact(req))

    async def new_session(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        return await self._call(request, lambda: self.stub.NewSession(pb.Empty()))

    async def list_sessions(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        return await self._call(request,
                                lambda: self.stub.ListSessions(pb.Empty()))

    async def load_session(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        text = await request.text()
        body = json.loads(text) if text else {}
        req = pb.LoadSessionRequest(
            session_id=str(body.get("session_id", "") or ""))
        return await self._call(request, lambda: self.stub.LoadSession(req))

    async def delete_session(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        text = await request.text()
        body = json.loads(text) if text else {}
        req = pb.DeleteSessionRequest(
            session_id=str(body.get("session_id", "") or ""))
        return await self._call(request,
                                lambda: self.stub.DeleteSession(req))

    async def _send(self, resp: web.StreamResponse, data: bytes) -> bool:
        try:
            await resp.write(data)
            return True
        except (ConnectionError, ClientError):
            return False

    async def events(self, request: web.Request):
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self.subs.add(q)

        def disconnected() -> bool:
            t = request.transport
            return t is None or t.is_closing()

        since = self._since(request)
        try:
            for frame in list(self.ring):
                seq = self._frame_seq(frame)
                if seq is not None and seq <= since:
                    continue
                if disconnected() or not await self._send(resp, frame):
                    break
            while not disconnected():
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    if not await self._send(resp, b": heartbeat\n\n"):
                        break
                    continue
                if not await self._send(resp, frame):
                    break
        finally:
            self.subs.discard(q)
        return resp

    @property
    def sse_base(self) -> str:
        """Base URL of the harness SSE server (strip the trailing /events)."""
        base = self.sse_url
        if base.endswith("/events"):
            base = base[: -len("/events")]
        return base

    async def _proxy_update(self, request: web.Request, sub: str):
        """Stream /update/<sub> through to the harness SSE server.

        The APK (and manifest) live on the mac; we just tunnel them back.
        Public (no token): releases are meant to be distributable to any
        phone. The file is streamed, not buffered, so a large APK doesn't
        eat bridge memory.
        """
        url = f"{self.sse_base}/update/{sub}"
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        return web.Response(
                            status=r.status, body=await r.read(),
                            headers={"Content-Type": "application/json"})
                    headers = {
                        "Content-Type":
                            r.headers.get("Content-Type",
                                          "application/octet-stream"),
                        "Cache-Control": "no-store",
                        "X-Accel-Buffering": "no",
                    }
                    cl = r.headers.get("Content-Length")
                    if cl:
                        headers["Content-Length"] = cl
                    resp = web.StreamResponse(status=200, headers=headers)
                    await resp.prepare(request)
                    async for chunk in r.content.iter_any():
                        if not await self._send(resp, chunk):
                            break
                    return resp
        except (ClientError, ConnectionError) as e:
            return web.Response(status=502, text=f"upstream {e!r}")

    async def update_manifest(self, request: web.Request):
        return await self._proxy_update(request, "manifest")

    async def update_file(self, request: web.Request):
        return await self._proxy_update(request,
                                        request.match_info["filename"])

    async def update_index(self, request: web.Request):
        """GET /update -> 302 to the latest release (public, no token).

        Fetch the manifest and bounce the client to /update/<latest>.apk so
        `curl -L https://host/update` downloads the newest build directly.
        """
        url = f"{self.sse_base}/update/manifest"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        return web.Response(status=404, text="no release yet")
                    data = await r.json()
        except (ClientError, ConnectionError):
            return web.Response(status=502, text="upstream unreachable")
        filename = data.get("filename")
        if not filename or int(data.get("version", 0) or 0) <= 0:
            return web.Response(status=404, text="no release yet")
        return web.Response(status=302,
                            headers={"Location": f"/update/{filename}"})

    async def upstream(self) -> None:
        while True:
            try:
                await self._pump_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[llm-bridge] upstream error: {e!r}; "
                      f"reconnect in 2s", flush=True)
            await asyncio.sleep(2)

    async def _pump_once(self) -> None:
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=45)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(self.sse_url) as r:
                    buf = b""
                    while True:
                        line = await r.content.readline()
                        if not line:
                            break
                        buf += line
                        if line == b"\n":
                            frame, buf = buf, b""
                            if frame.strip():
                                self.ring.append(frame)
                                for q in list(self.subs):
                                    q.put_nowait(frame)
        finally:
            up_ok = False
            try:
                await asyncio.wait_for(self.stub.Status(pb.Empty()), timeout=5)
                up_ok = True
            except Exception:
                pass
            kind = "harness_up" if up_ok else "harness_down"
            frame = (f'event: bridge\n'
                     f'data: {{"type": "bridge", "kind": "{kind}"}}\n\n').encode()
            for q in list(self.subs):
                q.put_nowait(frame)


def build_app(bridge: Bridge) -> web.Application:
    app = web.Application()
    app.router.add_get("/health",
                       lambda r: web.json_response({"ok": True}))
    app.router.add_post("/ask", bridge.ask)
    app.router.add_post("/stop", bridge.stop)
    app.router.add_post("/resume", bridge.resume)
    app.router.add_post("/status", bridge.status)
    app.router.add_get("/status", bridge.status)
    app.router.add_post("/get-messages", bridge.get_messages)
    app.router.add_get("/get-messages", bridge.get_messages)
    app.router.add_post("/settings", bridge.settings)
    app.router.add_post("/compact", bridge.compact)
    app.router.add_post("/new-session", bridge.new_session)
    app.router.add_get("/new-session", bridge.new_session)
    app.router.add_get("/list-sessions", bridge.list_sessions)
    app.router.add_post("/list-sessions", bridge.list_sessions)
    app.router.add_get("/load-session", bridge.load_session)
    app.router.add_post("/load-session", bridge.load_session)
    app.router.add_get("/delete-session", bridge.delete_session)
    app.router.add_post("/delete-session", bridge.delete_session)
    app.router.add_get("/update", bridge.update_index)
    app.router.add_get("/update/", bridge.update_index)
    app.router.add_get("/update/manifest", bridge.update_manifest)
    app.router.add_get("/update/{filename}", bridge.update_file)
    app.router.add_get("/events", bridge.events)
    return app


async def amain() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG
    cfg = json.load(open(path))
    port = int(cfg.get("port", 18830))
    bridge = Bridge(cfg)
    runner = web.AppRunner(build_app(bridge))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    print(f"[llm-bridge] listening on 127.0.0.1:{port} -> {bridge.grpc_target}",
          flush=True)
    up = asyncio.create_task(bridge.upstream())

    def watch(t: "asyncio.Task[None]") -> None:
        if not t.cancelled():
            print(f"[llm-bridge] upstream task ended: {t.exception()!r}",
                  flush=True)

    up.add_done_callback(watch)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    up.cancel()
    await bridge.channel.close()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(amain())