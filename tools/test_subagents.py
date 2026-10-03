"""Offline tests for subagents: scripted LLM streams, no network."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_harness.config import Settings            # noqa: E402
from llm_harness.events import EventBus           # noqa: E402
from llm_harness.harness import Harness           # noqa: E402
from llm_harness.llm import LLMClient             # noqa: E402
from llm_harness.subagents import (               # noqa: E402
    REPORT_CAP, SubagentRun, dispatch)

ROOT = tempfile.mkdtemp(prefix="subagent-test-")


def mksettings() -> Settings:
    s = Settings()
    s.api_key = "test-key"
    return s


# ---- llm.py event shapes (see LLMClient.stream docstring) ---------------
def text_ev(t: str) -> dict:
    return {"kind": "text", "text": t}


def finish(reason: str | None = None, usage: dict | None = None) -> dict:
    return {"kind": "finish", "reason": reason, "usage": usage}


def tool_ev(name: str, arguments: str, id: str = "call_1",
            index: int = 0) -> dict:
    return {"kind": "tool", "index": index, "id": id, "name": name,
            "arguments": arguments}


class ScriptedStream:
    """Per-client scripted turns, keyed by session id."""

    def __init__(self):
        self.counters: dict[str, int] = {}
        self.requests: dict[str, list[dict]] = {}

    def attach(self, client: LLMClient, turns: list[list[dict]]):
        sid = client.session_id
        self.counters.setdefault(sid, 0)
        self.requests.setdefault(sid, [])

        def stream(request: dict):
            self.requests[sid].append(request)
            i = self.counters[sid]
            self.counters[sid] = i + 1
            return iter(turns[i])

        async def gen(request: dict):
            for ev in stream(request):
                yield ev

        cleared: list[str] = []
        closed: list[str] = []

        async def close():
            closed.append(sid)

        async def admin_clear_session(session_id: str):
            cleared.append(session_id)
            return True

        async def admin_session(session_id: str):
            return {"in_flight": False}

        client.stream_with_retry = gen
        client.close = close
        client.admin_clear_session = admin_clear_session
        client.admin_session = admin_session
        return cleared


def drain_all(q) -> list[dict]:
    evs = []
    while True:
        try:
            evs.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return evs


def child_script():
    return [
        [tool_ev("read", '{"path":"a.txt"}'), finish("tool_calls"),
         finish(usage={"prompt_tokens": 10, "completion_tokens": 5})],
        [text_ev("FINDINGS: a.txt:1 hello"), finish("stop")],
    ]


# --------------------------------------------------------------- tests
async def test_happy_path():
    root = tempfile.mkdtemp(dir=ROOT)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("hello\n")
    events = EventBus()
    q = events.subscribe()
    sa = SubagentRun(root, mksettings(), events, asyncio.Event())
    ss = ScriptedStream()
    cleared = ss.attach(sa.client, child_script())

    report = await sa.run("explore", "find hello", "", 5)

    assert report == "FINDINGS: a.txt:1 hello", report
    assert sa.client.session_id != ""
    # request tool gating: explore = read + bash only
    names = [t["function"]["name"] for t in ss.requests[sa.client.session_id][0]["tools"]]
    assert names == ["read", "bash"], names
    # child settings overrides
    s = sa.client.s
    assert s.client_header == "llm-harness-subagent"
    assert s.max_context_tokens == 40_000
    assert s.max_output_tokens <= 16_384
    assert s.thinking_enabled is False
    assert s.thinking_effort == "minimal"
    assert s.temperature == 0.0
    # usage accumulated
    assert sa.usage["prompt_tokens"] == 10, sa.usage
    assert sa.usage["completion_tokens"] == 5
    # session cleared exactly once, on the child session
    assert cleared == [sa.client.session_id], cleared
    # transcript
    lines = [json.loads(l) for l in open(sa.transcript_path)]
    kinds = [l.get("type") or l.get("role") for l in lines]
    assert kinds == ["start", "user", "assistant", "tool", "assistant",
                     "report", "done"], kinds
    assert "hello" in lines[3]["content"]
    assert lines[2]["tool_calls"][0]["function"]["name"] == "read"
    # events
    evs = drain_all(q)
    types = [e["type"] for e in evs]
    assert "subagent_started" in types and "subagent_done" in types
    done = [e for e in evs if e["type"] == "subagent_done"][0]
    assert done["turns"] == 2 and done["tool_errors"] == 0, done
    print("ok test_happy_path")


async def test_stop_before_run():
    root = tempfile.mkdtemp(dir=ROOT)
    events = EventBus()
    q = events.subscribe()
    stop = asyncio.Event()
    stop.set()
    sa = SubagentRun(root, mksettings(), events, stop)
    ss = ScriptedStream()
    cleared = ss.attach(sa.client, child_script())
    report = await sa.run("explore", "find hello", "", 5)
    assert "stopped" in report and "(no output)" in report, report
    assert cleared == [sa.client.session_id]
    evs = drain_all(q)
    done = [e for e in evs if e["type"] == "subagent_done"][0]
    assert done["turns"] == 0
    print("ok test_stop_before_run")


async def test_unknown_tool_for_role():
    root = tempfile.mkdtemp(dir=ROOT)
    events = EventBus()
    q = events.subscribe()
    sa = SubagentRun(root, mksettings(), events, asyncio.Event())
    ss = ScriptedStream()
    ss.attach(sa.client, [
        [tool_ev("write", '{"path":"x.txt","content":"y"}'),
         finish("tool_calls")],
        [text_ev("FINDINGS: write not allowed"), finish("stop")],
    ])
    report = await sa.run("explore", "try to write", "", 5)
    assert report == "FINDINGS: write not allowed"
    assert sa.tool_errors == 1, sa.tool_errors
    lines = [json.loads(l) for l in open(sa.transcript_path)]
    tool_lines = [l for l in lines if l.get("role") == "tool"]
    assert tool_lines[0]["content"].startswith(
        "error: tool not available for role 'explore': write")
    drain_all(q)
    print("ok test_unknown_tool_for_role")


async def test_max_turns():
    root = tempfile.mkdtemp(dir=ROOT)
    events = EventBus()
    q = events.subscribe()
    sa = SubagentRun(root, mksettings(), events, asyncio.Event())
    ss = ScriptedStream()
    turn = [tool_ev("read", '{"path":"a.txt"}'), finish("tool_calls")]
    ss.attach(sa.client, [turn, turn])
    report = await sa.run("explore", "loop", "", 2)
    assert report.startswith("(subagent ended early: max_turns"), report
    assert sa.turns == 2
    drain_all(q)
    print("ok test_max_turns")


async def test_report_cap():
    root = tempfile.mkdtemp(dir=ROOT)
    events = EventBus()
    q = events.subscribe()
    sa = SubagentRun(root, mksettings(), events, asyncio.Event())
    ss = ScriptedStream()
    ss.attach(sa.client, [[text_ev("F" * 30_000), finish("stop")]])
    report = await sa.run("explore", "verbose", "", 5)
    assert len(report) == REPORT_CAP + len("\n...[report truncated]"), len(report)
    assert report.endswith("...[report truncated]")
    drain_all(q)
    print("ok test_report_cap")


async def test_dispatch_validation():
    root = tempfile.mkdtemp(dir=ROOT)
    events = EventBus()
    r = await dispatch(root, mksettings(), events, asyncio.Event(),
                       {"role": "nope", "task": "x"})
    assert r.startswith("error: unknown role"), r
    r = await dispatch(root, mksettings(), events, asyncio.Event(),
                       {"role": "explore", "task": "  "})
    assert r == "error: task instruction is empty", r
    r = await dispatch(root, mksettings(), events, asyncio.Event(),
                       {"role": "explore", "task": "x", "max_turns": "abc"})
    assert r == "error: max_turns is not a number", r
    print("ok test_dispatch_validation")


async def test_harness_integration():
    root = tempfile.mkdtemp(dir=ROOT)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("hello\n")
    h = Harness(root, mksettings())
    q = h.events.subscribe()
    ss = ScriptedStream()

    main_turns = [
        [tool_ev("task", json.dumps(
            {"role": "explore", "task": "find hello in a.txt",
             "max_turns": 5}), id="call_t"),
         finish("tool_calls")],
        [text_ev("DONE: report received"), finish("stop")],
    ]
    ss.attach(h.client, main_turns)
    child_turns = child_script()

    # intercept subagent client creation: LLMClient is constructed inside
    # SubagentRun, so patch the class and route by client_header
    import llm_harness.subagents as subs
    orig_init = subs.LLMClient.__init__

    def patched_init(self, s):
        orig_init(self, s)
        ss.attach(self, child_turns)
    subs.LLMClient.__init__ = patched_init
    try:
        res = await h.ask("find hello in a.txt")
        assert res["state"] == "running"
        await h._task
    finally:
        subs.LLMClient.__init__ = orig_init

    evs = drain_all(q)
    types = [e["type"] for e in evs]
    assert "main_paused" in types, types
    assert "subagent_started" in types and "subagent_done" in types
    paused = [e for e in evs if e["type"] == "main_paused"][0]
    assert paused["released"] is True

    # main history: system, user, assistant(task), tool(report), assistant
    roles = [m["role"] for m in h.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"], roles
    assert h.history[2]["tool_calls"][0]["function"]["name"] == "task"
    assert "FINDINGS: a.txt:1 hello" in h.history[3]["content"]
    assert h.history[4]["content"] == "DONE: report received"

    # both sessions cleared: main once, child once
    child_sid = [e for e in evs if e["type"] == "subagent_started"][0]["agent_id"]
    # subagent_started carries agent_id; the mtplx session id is in the transcript
    tdir = os.path.join(root, ".llm-harness", "subagents")
    tfiles = os.listdir(tdir)
    assert len(tfiles) == 1
    first = json.loads(open(os.path.join(tdir, tfiles[0])).readline())
    assert first["session_id"]
    st = await h.status()
    assert st["active_subagents"] == []
    print("ok test_harness_integration")


async def main():
    await test_happy_path()
    await test_stop_before_run()
    await test_unknown_tool_for_role()
    await test_max_turns()
    await test_report_cap()
    await test_dispatch_validation()
    await test_harness_integration()
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())