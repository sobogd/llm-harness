"""mtplx LLM client: request building (docs/MTPLX.md) + streaming parse."""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from .config import Settings


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ContextOverflowError(LLMError):
    pass


def _thinking_params(s: Settings) -> tuple[bool, str | None]:
    """docs/MTPLX.md: enable_thinking + reasoning_effort (qwen flat fields).

    Server accepts exactly low/medium/xhigh; 'minimal' == thinking off.
    """
    if not s.thinking_enabled or s.thinking_effort == "minimal":
        return False, None
    return True, (s.thinking_effort or None)


def build_request(history: list[dict], s: Settings,
                  tools: list[dict] | None = None) -> dict:
    """OpenAI-compatible chat/completions body, pi's mtplx mapping."""
    req: dict = {
        "model": s.model,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": s.max_output_tokens,
        "messages": history,
    }
    enabled, effort = _thinking_params(s)
    req["enable_thinking"] = enabled
    if effort:
        req["reasoning_effort"] = effort
    if s.temperature is not None:
        req["temperature"] = s.temperature
    if s.top_p is not None:
        req["top_p"] = s.top_p
    if s.top_k is not None:
        req["top_k"] = s.top_k
    if tools:
        req["tools"] = tools
    return req


def _error_from(chunk: dict, status: int | None) -> LLMError | None:
    err = chunk.get("error")
    if not err:
        return None
    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    low = msg.lower()
    if "context" in low or "too long" in low or "exceed" in low:
        return ContextOverflowError(msg, status)
    return LLMError(msg, status)


class LLMClient:
    # mtplx refuses a request for a session still held by a generation that
    # has not yet registered our disconnect as a cancel (HTTP 409, "already
    # in flight"). The engine picks the cancel up at the next decode round
    # and hands the session over (server wait is bounded, ~30 s), so a
    # client that was just stopped retries with backoff instead of failing.
    BUSY_STATUS = 409

    def __init__(self, s: Settings):
        self.s = s
        # Stable engine session id: pinning all requests of this client to one
        # mtplx session keeps the engine session bank from growing per request.
        self.session_id = uuid.uuid4().hex
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=10.0))

    @property
    def url(self) -> str:
        return self.s.base_url.rstrip("/") + "/v1/chat/completions"

    @property
    def headers(self) -> dict:
        return {
            "Content-Type": "application/json",   # mandatory, see MTPLX.md
            "Authorization": f"Bearer {self.s.api_key}",
            "x-mtplx-client": self.s.client_header,
            "x-mtplx-session-id": self.session_id,
        }

    def new_session_id(self) -> str:
        """Adopt a fresh mtplx session id for a different conversation.

        The previous id is orphaned: the server keeps its bank entry until
        idle-TTL or LRU eviction. Call admin_clear_session() on it first.
        """
        self.session_id = uuid.uuid4().hex
        return self.session_id

    async def close(self) -> None:
        await self._client.aclose()

    # -- streaming -----------------------------------------------------------
    async def stream(self, request: dict):
        """Yield parsed events:

        {"kind": "thinking", "text": ...}
        {"kind": "text",     "text": ...}
        {"kind": "tool",     "index": i, "id": ..., "name": ..., "arguments": ...}
        {"kind": "finish",   "reason": ..., "usage": {...} | None}
        """
        async with self._client.stream("POST", self.url, json=request,
                                       headers=self.headers) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                try:
                    data = json.loads(body)
                    err = _error_from(data, resp.status_code)
                except json.JSONDecodeError:
                    err = None
                raise (err or LLMError(body[:500], resp.status_code))
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                err = _error_from(chunk, resp.status_code)
                if err:
                    raise err
                if chunk.get("usage") is not None:
                    yield {"kind": "finish", "reason": None,
                           "usage": chunk["usage"]}
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content")
                    if rc:
                        yield {"kind": "thinking", "text": rc}
                    c = delta.get("content")
                    if c:
                        yield {"kind": "text", "text": c}
                    for tc in delta.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        yield {"kind": "tool",
                               "index": tc.get("index", 0),
                               "id": tc.get("id"),
                               "name": fn.get("name"),
                               "arguments": fn.get("arguments")}
                    if choice.get("finish_reason"):
                        yield {"kind": "finish",
                               "reason": choice["finish_reason"], "usage": None}

    async def stream_with_retry(self, request: dict, retries: int = 10):
        """stream() + retry on 409 'already in flight' (see class comment).

        A mid-stream 409 restarts the whole request: nothing from the torn
        attempt was appended to history by the caller, so retrying is safe.
        """
        for attempt in range(max(retries, 1)):
            try:
                async for ev in self.stream(request):
                    yield ev
                return
            except LLMError as e:
                if e.status == self.BUSY_STATUS and attempt < retries - 1:
                    await asyncio.sleep(3.0)
                    continue
                raise

    # -- mtplx admin (session bank) ------------------------------------------
    async def admin_session(self, session_id: str) -> dict | None:
        """Session bank entry for session_id (in_flight, KV bytes, ...) or None."""
        try:
            resp = await self._client.get(
                self.s.base_url.rstrip("/") + "/admin/sessions",
                headers=self.headers)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None
        for s in data.get("sessions") or []:
            if s.get("session_id") == session_id:
                return s
        return None

    async def admin_clear_session(self, session_id: str) -> bool:
        """Release a session's KV cache (server keeps it restorable)."""
        try:
            resp = await self._client.post(
                self.s.base_url.rstrip("/")
                + f"/admin/sessions/{session_id}/clear",
                headers=self.headers)
        except httpx.HTTPError:
            return False
        return resp.status_code < 400

    # -- one-shot (compaction) ----------------------------------------------
    async def complete(self, messages: list[dict], max_tokens: int = 2000,
                       enable_thinking: bool = False,
                       retries: int = 10) -> str:
        last: LLMError | None = None
        for attempt in range(max(retries, 1)):
            text, err = await self._complete_once(messages, max_tokens,
                                                  enable_thinking)
            if err is None:
                return text
            last = err
            if err.status == self.BUSY_STATUS and attempt < retries - 1:
                await asyncio.sleep(3.0)
                continue
            raise err
        raise last  # unreachable

    async def _complete_once(self, messages: list[dict], max_tokens: int,
                             enable_thinking: bool) -> tuple[str, LLMError | None]:
        req = {
            "model": self.s.model,
            "stream": False,
            "max_tokens": max_tokens,
            "enable_thinking": enable_thinking,
            "messages": messages,
        }
        if self.s.temperature is not None:
            req["temperature"] = self.s.temperature
        if self.s.top_p is not None:
            req["top_p"] = self.s.top_p
        if self.s.top_k is not None:
            req["top_k"] = self.s.top_k
        resp = await self._client.post(self.url, json=req, headers=self.headers)
        if resp.status_code != 200:
            body = resp.text
            try:
                err = _error_from(json.loads(body), resp.status_code)
            except json.JSONDecodeError:
                err = None
            return "", (err or LLMError(body[:500], resp.status_code))
        data = resp.json()
        return ((data.get("choices") or [{}])[0].get("message", {})
                .get("content") or ""), None
