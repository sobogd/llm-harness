"""Unit checks for LLMClient busy-retry (409) logic. No network."""
import asyncio
import llm_harness.llm as m
from llm_harness.llm import LLMClient, LLMError
from llm_harness.config import Settings

real_sleep = asyncio.sleep


def patched_sleep(s):
    return real_sleep(0.01)


def make_client():
    return LLMClient(Settings())


async def stream_409_twice(self, request):
    self.calls = getattr(self, "calls", 0) + 1
    if self.calls < 3:
        raise LLMError("session abc is already in flight", 409)
    yield {"kind": "text", "text": "ok"}
    yield {"kind": "finish", "reason": "stop", "usage": {"prompt_tokens": 5}}


def stream_500(self, request):
    async def gen():
        self.calls = getattr(self, "calls", 0) + 1
        raise LLMError("boom", 500)
        yield  # unreachable: makes this an async generator
    return gen()


async def main():
    m.asyncio.sleep = patched_sleep

    # 1) 409 x2 then success
    c = make_client()
    c.stream = stream_409_twice.__get__(c, LLMClient)
    evs = []
    async for e in c.stream_with_retry({}):
        evs.append(e)
    assert c.calls == 3, c.calls
    assert evs[-1]["kind"] == "finish"
    print("PASS stream: 409 retried, calls =", c.calls)

    # 2) non-409 is not retried
    c2 = make_client()
    c2.stream = stream_500.__get__(c2, LLMClient)
    raised = None
    try:
        async for e in c2.stream_with_retry({}):
            pass
    except LLMError as e:
        raised = e
    assert raised is not None and raised.status == 500
    assert c2.calls == 1, c2.calls
    print("PASS stream: 500 raised immediately, calls =", c2.calls)

    # 3) complete() retries 409 then succeeds
    c3 = make_client()

    async def comp_once(self, messages, max_tokens, enable_thinking):
        self.calls = getattr(self, "calls", 0) + 1
        if self.calls == 1:
            return "", LLMError("busy", 409)
        return "summary", None

    c3._complete_once = comp_once.__get__(c3, LLMClient)
    out = await c3.complete([{"role": "user", "content": "x"}], retries=4)
    assert out == "summary" and c3.calls == 2, (out, c3.calls)
    print("PASS complete: 409 retried, calls =", c3.calls)


asyncio.run(main())
print("ALL OK")
