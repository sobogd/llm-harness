"""Entry point: python -m llm_harness --cwd DIR --port-grpc 9000 --port-sse 9001"""
from __future__ import annotations

import argparse
import asyncio
import signal

import grpc
import uvicorn

from .config import Settings
from .grpc_server import serve as serve_grpc
from .harness import Harness
from .sse_server import create_app


async def main() -> None:
    ap = argparse.ArgumentParser(prog="llm_harness")
    ap.add_argument("--cwd", default=".", help="workspace root for tools")
    ap.add_argument("--port-grpc", type=int, default=9000)
    ap.add_argument("--port-sse", type=int, default=9001)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--max-context", type=int, default=60_000)
    ap.add_argument("--max-output", type=int, default=8_192)
    ap.add_argument("--no-thinking", action="store_true")
    ap.add_argument("--thinking-effort", default="medium")
    args = ap.parse_args()

    settings = Settings(
        max_context_tokens=args.max_context,
        max_output_tokens=args.max_output,
        thinking_enabled=not args.no_thinking,
        thinking_effort=args.thinking_effort,
    )
    if args.model:
        settings.model = args.model
    if args.base_url:
        settings.base_url = args.base_url
    settings.validate()

    harness = Harness(args.cwd, settings)
    await harness.startup()

    loop = asyncio.get_running_loop()
    grpc_server = serve_grpc(harness, args.port_grpc, loop)
    await grpc_server.start()
    print(f"[harness] gRPC listening on :{args.port_grpc}", flush=True)

    sse_app = create_app(harness)
    uvi_config = uvicorn.Config(sse_app, host="127.0.0.1",
                                port=args.port_sse, log_level="warning",
                                timeout_graceful_shutdown=10)
    uvi = uvicorn.Server(uvi_config)
    uvi_task = asyncio.create_task(uvi.serve())
    print(f"[harness] SSE listening on :{args.port_sse}", flush=True)
    print(f"[harness] session {harness.session_id}, root {args.cwd}", flush=True)

    stop_ev = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_ev.set)
        except NotImplementedError:
            pass

    await stop_ev.wait()
    print("[harness] shutting down", flush=True)
    await harness.aclose()
    await grpc_server.stop(grace=2)
    uvi.should_exit = True
    try:
        await asyncio.wait_for(uvi_task, timeout=15)
    except asyncio.TimeoutError:
        pass


if __name__ == "__main__":
    asyncio.run(main())