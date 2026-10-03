"""Subagents: isolated child agents for one self-contained task.

While a subagent runs, the parent session is paused: its mtplx KV cache
is released via ``/admin/sessions/{id}/clear`` so the machine's RAM serves
the child instead. The child runs on its own session (own id, own small
history, role-scoped tools, no parent system prompt) and is cleared when
done. Its final report comes back to the parent as the ``task`` tool
result; the parent resumes on the same session id (mtplx restores from
the SSD cache when it can). Subagents run strictly sequentially.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid

from .config import Settings
from .events import EventBus
from .llm import ContextOverflowError, LLMClient, LLMError, build_request
from .tools import TOOL_SCHEMAS, execute_tool

MAX_CONTEXT_TOKENS = 40_000    # child context cap (parent runs full-size)
REPORT_CAP = 20_000            # max chars of the report returned to parent
DEFAULT_MAX_TURNS = 20
MAX_TURNS_CAP = 40
RELEASE_WAIT_S = 90            # how long to wait for parent not-in-flight

WORKER_PROMPT = (
    "You are a worker subagent of llm-harness. You receive one "
    "self-contained task and have no other conversation context.\n"
    "- Complete the task using your tools. Tool paths are relative to the "
    "workspace root; bash runs in the workspace root (PATH includes "
    "/opt/homebrew/bin).\n"
    "- Read files before citing or editing them. Verify results (run code, "
    "re-read files) before claiming anything is done.\n"
    "- Do not ask questions. If context is missing, state your assumption "
    "and proceed.\n"
    "- Finish by replying with a final report (no tool calls):\n"
    "  FINDINGS: bullet list, each item with file:line or URL evidence\n"
    "  ACTIONS: what you changed or ran, and the result\n"
    "  GAPS: what you could not do and why\n"
    "Keep the report under 2000 words."
)

ROLE_TOOLS: dict[str, list[str]] = {
    "explore": ["read", "bash"],
    "research": ["read", "bash", "web_search", "browser_fetch"],
    "builder": [t["function"]["name"] for t in TOOL_SCHEMAS
                if t["function"]["name"] != "task"],
    "recon": ["read", "bash"],
    "deep": ["read", "bash"],
    "analyst": ["read", "bash"],
    "orchestrator": ["read", "bash"],
    "reviewer": ["read", "bash"],
}
_TOOL_BY_NAME = {t["function"]["name"]: t for t in TOOL_SCHEMAS}

ROLE_PROMPTS: dict[str, str] = {
    "recon": (
        "You are the recon stage of a task pipeline. You receive one user "
        "task and no other conversation context. Explore the workspace and "
        "find everything related to the task (files, modules, services, "
        "docs). Do NOT implement anything.\n"
        "Report:\n"
        "  RELEVANT: list of file paths with line refs, one line each on "
        "what it does\n"
        "  MECHANISMS: key mechanisms, configs, entry points involved\n"
        "  GAPS: what a deeper dive should investigate, and why\n"
        "Keep the report under 2000 words."
    ),
    "deep": (
        "You are the deep-dive stage of a task pipeline. You receive the "
        "task and a shortlist of relevant files from the recon stage. "
        "Investigate how the code actually works: control flow, data, "
        "dependencies, extension points. Do NOT implement anything.\n"
        "Report:\n"
        "  FINDINGS: how it works, each item with file:line evidence\n"
        "  CONSTRAINTS: invariants, coupling, risks to changing it\n"
        "  GAPS: what you could not determine and why\n"
        "Keep the report under 2000 words."
    ),
    "analyst": (
        "You are the analyst stage of a task pipeline. You receive the "
        "task, the recon findings and the deep-dive research. Produce a "
        "solution plan: concrete ordered steps, the files to change, "
        "risks, and a definition of done. Do NOT write the full "
        "implementation.\n"
        "Report:\n"
        "  PLAN: numbered steps, each with target files and expected "
        "outcome\n"
        "  RISKS: what could break and how to guard it\n"
        "  GAPS: open questions the orchestrator must resolve\n"
        "Keep the report under 2000 words."
    ),
    "orchestrator": (
        "You are the orchestrator stage of a task pipeline. You receive a "
        "plan. Split it into N independent, self-contained task specs. "
        "Each spec must be fully self-contained: goal, exact files/lines to "
        "touch, constraints, and a definition of done - a builder with no "
        "other conversation context must be able to execute it. Do NOT "
        "implement anything.\n"
        "Report:\n"
        "  SPECS: numbered list, each labelled S1, S2, ... with goal, "
        "files, constraints, done-criteria\n"
        "  DEPENDENCIES: which specs must run in which order\n"
        "Keep the report under 2000 words."
    ),
    "reviewer": (
        "You are the reviewer stage of a task pipeline. You receive the "
        "task, the plan and reports of what was implemented. Review the "
        "changed code for correctness, task compliance, regressions and "
        "quality. Read the changed files; run checks when useful. Do NOT "
        "implement fixes.\n"
        "Report:\n"
        "  VERDICT: PASS or FAIL\n"
        "  ISSUES: list, each with file:line, severity, what is wrong\n"
        "  GAPS: anything you could not verify\n"
        "Keep the report under 2000 words."
    ),
}


class SubagentRun:
    """One child agent: new session, small context, role-scoped tools."""

    def __init__(self, root: str, parent: Settings, events: EventBus,
                 stop: asyncio.Event, tool_call_id: str = ""):
        self.root = root
        self.events = events
        self.stop = stop
        self.tool_call_id = tool_call_id
        self.id = str(uuid.uuid4())
        s = dataclasses.replace(parent)
        s.max_context_tokens = min(s.max_context_tokens, MAX_CONTEXT_TOKENS)
        s.max_output_tokens = min(s.max_output_tokens, 16_384)
        s.temperature = 0.0
        s.top_p = None
        s.top_k = None
        s.thinking_enabled = False
        s.thinking_effort = "minimal"
        s.client_header = "llm-harness-subagent"
        s.validate()
        self.client = LLMClient(s)
        self.transcript_path = None
        self.turns = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.tool_errors = 0

    # ------------------------------------------------------------- transcript
    def _log(self, obj: dict) -> None:
        if self.transcript_path is None:
            return
        line = json.dumps(obj, ensure_ascii=False)
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _start_log(self, role: str, task: str, context: str) -> None:
        from pathlib import Path
        d = Path(self.root) / ".llm-harness" / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        self.transcript_path = d / f"{self.id}.jsonl"
        self._log({"type": "start", "id": self.id,
                   "session_id": self.client.session_id, "role": role,
                   "ts": int(time.time() * 1000)})
        self._log({"role": "user", "content":
                   f"TASK: {task}\n\nCONTEXT:\n{context}" if context
                   else f"TASK: {task}"})

    # ------------------------------------------------------------------- run
    async def run(self, role: str, task: str, context: str,
                  max_turns: int) -> str:
        """Execute the child to completion; returns the report text."""
        tools = [_TOOL_BY_NAME[n] for n in ROLE_TOOLS[role]]
        user_msg = (f"TASK: {task}\n\nCONTEXT:\n{context}" if context
                    else f"TASK: {task}")
        history = [{"role": "system",
                    "content": ROLE_PROMPTS.get(role, WORKER_PROMPT)},
                   {"role": "user", "content": user_msg}]
        self._start_log(role, task, context)
        await self.events.publish(
            "subagent_started", agent_id=self.id, role=role,
            tools=ROLE_TOOLS[role], task_preview=task[:200],
            tool_call_id=self.tool_call_id)
        last_text = ""
        try:
            for turn in range(1, max_turns + 1):
                if self.stop.is_set():
                    return await self._end("stopped", last_text)
                self.turns = turn
                await self.events.publish("subagent_turn_started",
                     agent_id=self.id, turn=turn,
                     tool_call_id=self.tool_call_id)
                thinking = text = ""
                tool_calls: dict[int, dict] = {}
                finish_reason = None

                def merge_tool(ev: dict) -> None:
                    i = int(ev.get("index") or 0)
                    slot = tool_calls.setdefault(
                        i, {"id": None, "name": "", "args": ""})
                    if ev.get("id"):
                        slot["id"] = ev["id"]
                    if ev.get("name"):
                        slot["name"] += ev["name"]
                    slot["args"] += ev.get("arguments") or ""

                try:
                    async for ev in self.client.stream_with_retry(
                            build_request(history, self.client.s, tools)):
                        if self.stop.is_set():
                            break
                        k = ev["kind"]
                        if k == "thinking":
                            thinking += ev.get("text") or ""
                            await self.events.publish(
                                "subagent_thinking_delta", agent_id=self.id,
                                text=ev.get("text") or "",
                                tool_call_id=self.tool_call_id)
                        elif k == "text":
                            text += ev.get("text") or ""
                            await self.events.publish("subagent_text_delta",
                                                     agent_id=self.id,
                                                     text=ev.get("text") or "",
                                                     tool_call_id=self.tool_call_id)
                        elif k == "tool":
                            merge_tool(ev)
                        elif k == "finish":
                            if ev.get("usage"):
                                u = ev["usage"]
                                self.usage["prompt_tokens"] += \
                                    int(u.get("prompt_tokens") or 0)
                                self.usage["completion_tokens"] += \
                                    int(u.get("completion_tokens") or 0)
                            if ev.get("reason"):
                                finish_reason = ev["reason"]
                except ContextOverflowError as e:
                    return await self._end("context_overflow", last_text,
                                           str(e))
                except LLMError as e:
                    return await self._end("error", last_text, str(e))
                if self.stop.is_set():
                    if text or thinking:
                        m = self._assistant_msg(thinking, text, tool_calls)
                        self._log(m)
                        history.append(m)
                    return await self._end("stopped", last_text)
                msg = self._assistant_msg(thinking, text, tool_calls)
                self._log(msg)
                history.append(msg)
                last_text = text
                await self.events.publish("subagent_turn_finished",
                     agent_id=self.id, turn=turn,
                     reason=finish_reason or "stop",
                     tool_call_id=self.tool_call_id)
                if finish_reason == "tool_calls" and tool_calls:
                    allowed = set(ROLE_TOOLS[role])
                    for i in sorted(tool_calls):
                        tc = tool_calls[i]
                        try:
                            args = json.loads(tc["args"] or "{}") or {}
                        except json.JSONDecodeError:
                            args = {}
                        await self.events.publish(
                            "subagent_tool_start", agent_id=self.id,
                            tool=tc["name"], args=args,
                            tool_call_id=self.tool_call_id)
                        t0 = time.monotonic()
                        if tc["name"] not in allowed:
                            result = (f"error: tool not available for "
                                      f"role '{role}': {tc['name']}")
                        else:
                            result = await execute_tool(self.root, tc["name"],
                                                       args)
                        if result.startswith("error:"):
                            self.tool_errors += 1
                        await self.events.publish(
                            "subagent_tool_end", agent_id=self.id,
                            tool=tc["name"],
                            duration_ms=int((time.monotonic() - t0) * 1000),
                            preview=result[:200],
                            tool_call_id=self.tool_call_id)
                        self._log({"role": "tool", "name": tc["name"],
                                   "tool_call_id": tc["id"],
                                   "content": result})
                        history.append({"role": "tool",
                                        "tool_call_id": tc["id"],
                                        "content": result})
                    continue
                if finish_reason == "length":
                    self._log({"role": "user",
                               "content":
                               "Your response was cut off at max_tokens. "
                               "Continue from exactly where you stopped."})
                    history.append({"role": "user",
                                    "content":
                                    "Your response was cut off at "
                                    "max_tokens. Continue from exactly where "
                                    "you stopped."})
                    continue
                return await self._end("done", last_text)
            return await self._end("max_turns", last_text)
        finally:
            await self.client.admin_clear_session(self.client.session_id)
            await self.client.close()
            self._log({"type": "done", "turns": self.turns,
                       "usage": self.usage, "tool_errors": self.tool_errors,
                       "ts": int(time.time() * 1000)})
            await self.events.publish(
                "subagent_done", agent_id=self.id, turns=self.turns,
                usage=self.usage, tool_errors=self.tool_errors,
                tool_call_id=self.tool_call_id)

    def _assistant_msg(self, thinking: str, text: str,
                       tool_calls: dict[int, dict]) -> dict:
        msg: dict = {"role": "assistant", "content": text or None}
        if thinking:
            msg["reasoning_content"] = thinking
        if tool_calls:
            msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": tc["args"] or "{}"}}
                for _, tc in sorted(tool_calls.items())]
        return msg

    async def _end(self, reason: str, text: str, detail: str = "") -> str:
        """Assemble the capped report returned to the parent."""
        if reason == "done":
            report = text or "(subagent returned no text)"
        else:
            head = f"(subagent ended early: {reason}"
            if detail:
                head += f"; {detail[:300]}"
            report = f"{head})\n" + ((text or "").strip()
                                     or "(no output)")
        if len(report) > REPORT_CAP:
            report = report[:REPORT_CAP] + "\n...[report truncated]"
        self._log({"type": "report", "reason": reason, "report": report})
        return report


async def dispatch(root: str, parent: Settings, events: EventBus,
                   stop: asyncio.Event, args: dict,
                   registry: list | None = None,
                   tool_call_id: str = "") -> str:
    """Validate task-tool arguments and run one subagent (sequential)."""
    role = (args.get("role") or "").strip()
    if role not in ROLE_TOOLS:
        return (f"error: unknown role: {role!r} (expected one of: "
                f"{', '.join(ROLE_TOOLS)})")
    task = (args.get("task") or "").strip()
    if not task:
        return "error: task instruction is empty"
    try:
        max_turns = min(int(args.get("max_turns") or DEFAULT_MAX_TURNS),
                        MAX_TURNS_CAP)
    except (TypeError, ValueError):
        return "error: max_turns is not a number"
    sa = SubagentRun(root, parent, events, stop,
                     tool_call_id=tool_call_id)
    if registry is not None:
        registry.append(sa.id)
    try:
        return await sa.run(role, task, (args.get("context") or "").strip(),
                            max_turns)
    except Exception as e:
        await sa.client.close()
        return f"error: subagent failed: {e}"
    finally:
        if registry is not None and sa.id in registry:
            registry.remove(sa.id)