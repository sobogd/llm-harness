"""Unit tests for JSONL persistence (phase 6): replay, snapshot, flush."""
import asyncio, sys, tempfile, json
sys.path.insert(0, ".")
from llm_harness.persistence import SessionStore

def test_replay():
    d = tempfile.mkdtemp()
    s1 = SessionStore(d)
    s1.record({"type": "meta", "session_id": "sid-1"})
    s1.record_message({"role": "system", "content": "sys"})
    s1.record_message({"role": "user", "content": "hi"})
    s1.record_message({"role": "assistant", "content": "", "tool_calls":
                       [{"id": "t1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]})
    s1.record_message({"role": "tool", "tool_call_id": "t1", "content": "ok"})
    s1.record_message({"role": "assistant", "content": "done"})
    s1.record_run("run1", "done", 2, "")
    asyncio.run(s1.close())
    assert s1.written == 7, s1.written

    s2 = SessionStore(d)
    d2 = s2.load()
    assert d2 is not None
    assert d2["meta"]["session_id"] == "sid-1"
    assert len(d2["history"]) == 5, len(d2["history"])
    assert d2["last_run"]["state"] == "done"
    assert s2.loaded_messages == 5
    print("replay: OK")

def test_history_snapshot():
    d = tempfile.mkdtemp()
    s1 = SessionStore(d)
    for i in range(5):
        s1.record_message({"role": "user", "content": f"m{i}"})
    s1.record_history([{"role": "system", "content": "sys"},
                       {"role": "user", "content": "kept-1"},
                       {"role": "user", "content": "kept-2"}])
    s1.record_message({"role": "assistant", "content": "after"})
    s1.record_run("r", "done", 1, "")
    asyncio.run(s1.close())

    s2 = SessionStore(d)
    d2 = s2.load()
    # snapshot replaces the 5 pre-compact messages
    assert [m["content"] for m in d2["history"]] == ["sys", "kept-1",
                                                     "kept-2", "after"], d2["history"]
    print("history snapshot: OK")

def test_fresh():
    d = tempfile.mkdtemp()
    s = SessionStore(d)
    assert s.load() is None
    print("fresh: OK")

def test_flush():
    d = tempfile.mkdtemp()
    s = SessionStore(d)
    for i in range(3):
        s.record_message({"role": "user", "content": f"x{i}"})
    async def go():
        await s.flush()
        assert s.written == 3, s.written
        lines = open(s.path).read().strip().splitlines()
        assert len(lines) == 3
        await s.close()
    asyncio.run(go())
    print("flush: OK")

if __name__ == "__main__":
    test_replay(); test_history_snapshot(); test_fresh(); test_flush()
    print("ALL OK")