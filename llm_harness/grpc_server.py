"""gRPC service implementation (proto/harness.proto)."""
from __future__ import annotations

import grpc

from . import llm_harness_pb2 as pb
from . import llm_harness_pb2_grpc as pb_grpc
from .harness import Harness


def _settings_from_proto(p: pb.Settings) -> dict:
    return {
        "max_context_tokens": p.max_context_tokens,
        "max_output_tokens": p.max_output_tokens,
        "thinking_enabled": p.thinking_enabled,
        "thinking_effort": p.thinking_effort,
        "sampling_temperature_x100": p.sampling_temperature_x100,
        "sampling_top_p_x100": p.sampling_top_p_x100,
        "sampling_top_k": p.sampling_top_k,
    }


def _settings_to_proto(s) -> pb.Settings:
    return pb.Settings(
        max_context_tokens=s.max_context_tokens,
        max_output_tokens=s.max_output_tokens,
        thinking_enabled=s.thinking_enabled,
        thinking_effort=s.thinking_effort,
        sampling_temperature_x100=int((s.temperature or 0) * 100),
        sampling_top_p_x100=int((s.top_p or 0) * 100),
        sampling_top_k=s.top_k or 0,
    )


class HarnessServicer(pb_grpc.HarnessServicer):
    def __init__(self, harness: Harness):
        self.h = harness

    def Ask(self, req, ctx) -> pb.AskReply:
        try:
            r = asyncio_run(self.h.ask(req.prompt, req.system_note))
        except ValueError as e:
            ctx.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        return pb.AskReply(run_id=r["run_id"], session_id=r["session_id"],
                           state=r["state"])

    def Stop(self, req, ctx) -> pb.RunStatus:
        asyncio_run(self.h.stop())
        return _run_status(self.h)

    def Resume(self, req, ctx) -> pb.RunStatus:
        asyncio_run(self.h.resume())
        return _run_status(self.h)

    def SetSettings(self, req, ctx) -> pb.Settings:
        try:
            s = asyncio_run(self.h.set_settings(_settings_from_proto(req)))
        except ValueError as e:
            ctx.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        return _settings_to_proto(s)

    def Compact(self, req, ctx) -> pb.CompactReply:
        try:
            info = asyncio_run(self.h.compact_request(req.keep_last_messages))
        except ValueError as e:
            ctx.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        return pb.CompactReply(
            ok=info.get("ok", False),
            tokens_before=info.get("tokens_before", 0),
            tokens_after=info.get("tokens_after", 0),
            messages_before=info.get("messages_before", 0),
            messages_after=info.get("messages_after", 0),
            summary_preview=info.get("summary_preview", ""))

    def NewSession(self, req, ctx) -> pb.AskReply:
        try:
            r = asyncio_run(self.h.new_session())
        except ValueError as e:
            ctx.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
        return pb.AskReply(session_id=r["session_id"], state="idle")

    def ListSessions(self, req, ctx) -> pb.ListSessionsReply:
        try:
            r = asyncio_run(self.h.list_sessions())
        except Exception as e:
            ctx.abort(grpc.StatusCode.INTERNAL, str(e))
        return pb.ListSessionsReply(
            sessions=[pb.SessionInfo(**s) for s in r["sessions"]])

    def LoadSession(self, req, ctx) -> pb.AskReply:
        try:
            r = asyncio_run(self.h.load_session(req.session_id))
        except ValueError as e:
            msg = str(e)
            ctx.abort(
                grpc.StatusCode.NOT_FOUND if "not found" in msg
                else grpc.StatusCode.FAILED_PRECONDITION, msg)
        except Exception as e:
            ctx.abort(grpc.StatusCode.INTERNAL, str(e))
        return pb.AskReply(session_id=r["session_id"], state=r["state"])

    def DeleteSession(self, req, ctx) -> pb.DeleteSessionReply:
        try:
            r = asyncio_run(self.h.delete_session(req.session_id))
        except ValueError as e:
            msg = str(e)
            ctx.abort(
                grpc.StatusCode.NOT_FOUND if "not found" in msg
                else grpc.StatusCode.FAILED_PRECONDITION, msg)
        except Exception as e:
            ctx.abort(grpc.StatusCode.INTERNAL, str(e))
        return pb.DeleteSessionReply(ok=r.get("ok", True),
                                     deleted=r.get("deleted", ""),
                                     session_id=r.get("session_id", ""))

    def RenameSession(self, req, ctx) -> pb.RenameSessionReply:
        try:
            r = asyncio_run(self.h.rename_session(req.session_id, req.name))
        except ValueError as e:
            msg = str(e)
            ctx.abort(
                grpc.StatusCode.NOT_FOUND if "not found" in msg
                else grpc.StatusCode.INVALID_ARGUMENT, msg)
        except Exception as e:
            ctx.abort(grpc.StatusCode.INTERNAL, str(e))
        return pb.RenameSessionReply(ok=r.get("ok", True),
                                     error="")

    def Status(self, req, ctx) -> pb.StatusReply:
        st = asyncio_run(self.h.status())
        return pb.StatusReply(
            settings=_settings_to_proto(self.h.settings),
            run=pb.RunStatus(
                run_id=st["run"]["run_id"],
                state={"idle": pb.RUN_STATE_IDLE, "running": pb.RUN_STATE_RUNNING,
                       "stopped": pb.RUN_STATE_STOPPED, "done": pb.RUN_STATE_DONE,
                       "error": pb.RUN_STATE_ERROR}[st["run"]["state"]],
                turn=st["run"]["turn"],
                last_error=st["run"]["last_error"]),
            session_id=st["session_id"],
            history_messages=st["history_messages"],
            prompt_tokens_last=st["prompt_tokens_last"],
            queue_depth=st["queue_depth"],
            started_at_unix_ms=st["started_at_unix_ms"],
            history_loaded=self.h._loaded is not None,
            loaded_from=self.h.store.loaded_from or "",
            persisted_messages=len(self.h.history),
            active_subagents=list(st["active_subagents"]))

    def GetMessages(self, req, ctx) -> pb.GetMessagesReply:
        names = {}
        for m in self.h.history:
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = tc["function"]["name"]
        msgs = self.h.get_messages(req.last)
        out = []
        for m in msgs:
            out.append(pb.Message(
                role=m["role"],
                content=m.get("content") or "",
                tool_calls=[pb.ToolCall(id=tc["id"],
                                        name=tc["function"]["name"],
                                        arguments=tc["function"]["arguments"])
                            for tc in m.get("tool_calls") or []],
                tool_call_id=m.get("tool_call_id") or "",
                name=names.get(m.get("tool_call_id") or "", ""),
                reasoning_content=m.get("reasoning_content") or ""))
        return pb.GetMessagesReply(m=out, model=self.h.settings.model)


def _run_status(h: Harness) -> pb.RunStatus:
    return pb.RunStatus(
        run_id=h.run_id or "",
        state={"idle": pb.RUN_STATE_IDLE, "running": pb.RUN_STATE_RUNNING,
               "stopped": pb.RUN_STATE_STOPPED, "done": pb.RUN_STATE_DONE,
               "error": pb.RUN_STATE_ERROR}[h.state],
        turn=h.turn,
        last_error=h.last_error or "")


def asyncio_run(coro):
    """The gRPC (sync) servicer runs on its own executor threads; the harness
    asyncio loop runs on the main thread. Bridge via run_coroutine_threadsafe."""
    import asyncio
    global _LOOP
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result()


_LOOP: "asyncio.AbstractEventLoop | None" = None  # set in __main__


def serve(harness: Harness, port: int, loop) -> grpc.aio.Server:
    global _LOOP
    _LOOP = loop
    server = grpc.aio.server()
    pb_grpc.add_HarnessServicer_to_server(HarnessServicer(harness), server)
    server.add_insecure_port(f"[::]:{port}")
    return server