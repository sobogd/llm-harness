"""Smoke test: tools + llm client + compaction primitives (no server needed)."""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llm_harness.tools import tool_read, tool_write, tool_edit, tool_bash
from llm_harness.llm import LLMClient, build_request
from llm_harness.config import Settings


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    return cond


ok = True
root = tempfile.mkdtemp()

# write
r = tool_write(root, {"path": "a/hello.py", "content": "print('hello harness')\n"})
ok &= check("write", r.startswith("Wrote"), r)

# read
r = tool_read(root, {"path": "a/hello.py"})
ok &= check("read numbered", r.startswith("   1| print('hello harness')"), r)

# read offset/limit
r = tool_read(root, {"path": "a/hello.py", "offset": 1, "limit": 1})
ok &= check("read limit", r.splitlines()[-1] == "(2 lines total)", r)

# edit exact
r = tool_edit(root, {"path": "a/hello.py", "oldText": "hello harness", "newText": "hello world"})
ok &= check("edit", "1 replacement" in r, r)

# edit non-unique
tool_write(root, {"path": "b.txt", "content": "x\nx\n"})
r = tool_edit(root, {"path": "b.txt", "oldText": "x", "newText": "y"})
ok &= check("edit non-unique rejected", r.startswith("error: oldText matches 2"), r)

# edit missing
r = tool_edit(root, {"path": "b.txt", "oldText": "zzz", "newText": "y"})
ok &= check("edit missing rejected", r.startswith("error: oldText not found"), r)

# path escape
r = tool_read(root, {"path": "../etc/passwd"})
ok &= check("path escape blocked", r.startswith("error: path escapes"), r)

# bash
r = asyncio.run(tool_bash(root, {"command": "echo out && echo err 1>&2 && exit 3"}))
ok &= check("bash rc+output", "exit code: 3" in r and "out" in r and "err" in r, r)

r = asyncio.run(tool_bash(root, {"command": "sleep 5", "timeout": 1}))
ok &= check("bash timeout", "timed out" in r, r)

# request building
s = Settings()
req = build_request([{"role": "system", "content": "x"}], s, tools=None)
ok &= check("req max_tokens", req["max_tokens"] == 8192)
ok &= check("req thinking", req["enable_thinking"] is True and req["reasoning_effort"] == "medium")
ok &= check("req stream_options", req["stream_options"] == {"include_usage": True})
s2 = Settings(thinking_enabled=False)
req2 = build_request([], s2)
ok &= check("req thinking off", req2["enable_thinking"] is False and "reasoning_effort" not in req2)
s3 = Settings(thinking_effort="minimal")
req3 = build_request([], s3)
ok &= check("req minimal==off", req3["enable_thinking"] is False)

# validation
try:
    Settings(max_context_tokens=300000).validate()
    ok &= check("validate > window rejected", False)
except ValueError:
    ok &= check("validate > window rejected", True)

print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)