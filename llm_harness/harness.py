"""Harness core: agent loop + run state machine + settings + compaction triggers."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from .compaction import compact
from .config import Settings
from .envinfo import environment_report
from .events import EventBus
from .llm import ContextOverflowError, LLMClient, LLMError, build_request
from .persistence import SessionStore
from . import subagents
from .tools import TOOL_SCHEMAS, execute_tool

RULES_FILE = os.path.expanduser("~/work/AGENTS.md")

CORE_PROMPT = (
    "You are llm-harness, a coding agent working in a local workspace (a "
    "file directory). "
    "You complete tasks by calling tools: read, write, edit, bash.\n"
    "- Read files before editing or judging them; edit requires an exact, "
    "unique oldText match, so copy text verbatim from read output.\n"
    "- Use bash for running code, tests, builds, and git. Prefer small, "
    "verifiable steps; run tests after changes.\n"
    "- Do not claim a task is done without verifying it (run it, read the "
    "result).\n"
    "Workspace root is the directory all tool paths are relative to."
)


DELEGATION_PROMPT = (
    "Delegation (task tool):\n"
    "- If a task is multi-part, needs broad research, codebase search, or "
    "would fill your context with intermediate output - break it into items "
    "and run each item with the task tool: one subagent per item, in "
    "sequence.\n"
    "- Write each instruction in English, fully self-contained: exact goal, "
    "which files/URLs to use, and the report format you need. You receive "
    "only the report, never the subagent's steps, so pass any context the "
    "subagent needs (file paths, snippets, earlier findings) in the context "
    "parameter.\n"
    "- The main session is paused while a subagent runs and resumes "
    "automatically with the report.\n"
)

TASK_MODE_PROMPT = (
    "TASK MODE (trigger: the user message starts with /task or /задача):\n"
    "Only when the message starts with `/task` or `/задача`, run the full "
    "pipeline; everything after the trigger is the task. Never run this "
    "pipeline for any other message.\n"
    "Stages - each is one `task` call, run strictly in order, wait for the "
    "report before the next, and pass prior reports in the `context` "
    "parameter:\n"
    "1. `recon`: locate every part of the project related to the task.\n"
    "2. `deep`: investigate how that code actually works.\n"
    "3. `analyst`: turn the findings into a concrete plan.\n"
    "4. `orchestrator`: split the plan into independent self-contained "
    "specs (S1, S2, ...).\n"
    "5. For each spec, in dependency order: `builder` implements it.\n"
    "6. `reviewer`: review the whole result against the task.\n"
    "7. You run build/tests/lint yourself via bash.\n"
    "Loops:\n"
    "- Review loop: if `reviewer` returns FAIL, re-run only the affected "
    "specs (step 5) and re-review. At most 2 review rounds; after that, "
    "report the remaining issues and stop.\n"
    "- Build loop: if build/tests/lint (step 7) fail, fix the errors (re-run "
    "the relevant builder, or edit directly) and re-run the checks. Repeat "
    "until it is green - this loop has no fixed cap; stop only when the "
    "build passes or you are clearly blocked.\n"
    "Keep every subagent call fully self-contained and written in English."
)


def load_rules(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
    except OSError:
        return ""
    return "\n".join(ln for ln in lines if ln)


def build_system_prompt() -> str:
    rules = load_rules(RULES_FILE)
    env = environment_report()
    return (CORE_PROMPT
            + DELEGATION_PROMPT
            + TASK_MODE_PROMPT
            + ("\n" + env if env else "")
            + ("\nRules:\n" + rules if rules else ""))


class RunStopped(Exception):
    pass


class Harness:
    def __init__(self, root: str, settings: Settings | None = None):
        self.root = root
        self.settings = settings or Settings()
        self.settings.validate()
        self.events = EventBus()
        self.client = LLMClient(self.settings)
        self.session_id = str(uuid.uuid4())
        self.system_prompt = build_system_prompt()
        self.history: list[dict] = [{"role": "system", "content": self.system_prompt}]
        self.state = "idle"            # idle | running | stopped | done | error
        self.run_id: str | None = None
        self.turn = 0
        self.queue: list[str] = []
        self.last_error: str | None = None
        self.last_prompt_tokens = 0
        self.last_usage: dict = {}
        self.started_at_ms = int(time.time() * 1000)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._turn_active = asyncio.Event()   # set while an LLM turn is in flight
        self._compact_in_flight = asyncio.Lock()
        self._active_subagents: list[str] = []
        # ---- phase 6: JSONL persistence + restore
        self.store = SessionStore(root)
        self._apply_loaded(self.store.load())

    def _apply_loaded(self, data: dict | None) -> None:
        """Adopt a replayed session (startup restore or LoadSession)."""
        self._loaded = data
        if not data or not data["history"]:
            return
        self.history = list(data["history"])
        self._drop_dangling_tool_calls()
        meta = data.get("meta") or {}
        if meta.get("session_id"):
            self.session_id = meta["session_id"]
        if meta.get("mtplx_session_id"):
            # reuse the exact mtplx session id this file ran on: its KV
            # snapshot (RAM or SSD) then prefix-matches the replayed history
            self.client.session_id = meta["mtplx_session_id"]
        lr = data.get("last_run")
        if lr is None:
            # fresh session (meta + system prompt only): nothing to resume
            self.state = "idle"
        elif lr.get("state") in ("done", "error"):
            self.state = "idle"            # finished run: new Ask continues
            self.last_error = lr.get("error") or None
        else:
            # torn run (crash/kill before run_done) — resumable
            self.state = "stopped"
            self.last_error = (lr or {}).get("error") or \
                "interrupted by server restart"
            if (lr or {}).get("run_id"):
                self.run_id = lr["run_id"]
            self.turn = (lr or {}).get("turn") or 0

    async def startup(self) -> None:
        """Called once from main before the servers start."""
        if self._loaded:
            await self.events.publish("session_loaded",
                                      path=self.store.loaded_from or "",
                                      messages=len(self.history),
                                      state=self.state)
        else:
            self.store.record({"type": "meta",
                               "session_id": self.session_id,
                               "mtplx_session_id": self.client.session_id,
                               "model": self.settings.model,
                               "created": int(time.time() * 1000),
                               "settings": self.settings.to_dict()})
            await self._sweep_orphans()
            # the system prompt lives in history but is never appended by
            # ask() — persist it explicitly or replay would lose it
            self.store.record_message(self.history[0])

    async def _sweep_orphans(self) -> None:
        """Clear mtplx RAM for sessions this workspace ran before but no
        longer uses (left behind by previous harness runs)."""
        current = self.client.session_id
        seen: set[str] = set()

        def scan(path) -> None:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "meta":
                    mid = rec.get("mtplx_session_id")
                    if mid:
                        seen.add(mid)
                    return

        scan(self.store.path)
        for f in sorted(self.store.sessions_dir.glob("*.jsonl")):
            scan(f)
        for mid in seen - {current}:
            try:
                await self.client.admin_clear_session(mid)
            except Exception:
                pass

    def _set_state(self, st: str) -> None:
        import sys
        print(f"[harness] state {self.state} -> {st} (run={str(self.run_id)[:8]} "
              f"turn={self.turn} task={self._task is not None and not self._task.done()})",
              file=sys.stderr, flush=True)
        self.state = st

    # ------------------------------------------------------------------ Ask
    async def ask(self, prompt: str, system_note: str = "") -> dict:
        if not prompt.strip():
            raise ValueError("prompt is empty")
        if self.state == "running":
            self.queue.append(prompt)
            await self.events.publish("queued", run_id=self.run_id,
                                      queue_depth=len(self.queue))
            return {"run_id": self.run_id, "session_id": self.session_id,
                    "state": "queued"}
        if system_note:
            m = {"role": "user", "content": system_note}
            self.history.append(m)
            self.store.record_message(m)
        m = {"role": "user", "content": prompt}
        self.history.append(m)
        self.store.record_message(m)
        self.run_id = str(uuid.uuid4())
        self.turn = 0
        self.last_error = None
        self.state = "running"
        self.store.record_run(self.run_id, "running", 0, "")
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        await self.events.publish("run_started", run_id=self.run_id,
                                  session_id=self.session_id)
        return {"run_id": self.run_id, "session_id": self.session_id,
                "state": "running"}

    # ----------------------------------------------------------------- Stop
    async def stop(self) -> None:
        if self.state != "running":
            return
        self._stop.set()
        self._set_state("stopped")
        self.last_error = "stopped by user"
        await self.events.publish("run_stopped", run_id=self.run_id,
                                  turn=self.turn)
        # Wait for the run task to actually finish: the loop breaks at the
        # next stream event (server sends at ~1/s) and closes the httpx
        # connection, so mtplx sees the disconnect and releases the session.
        # Without this, a fast Resume would spawn a SECOND _run task while the
        # first one still owns the mtplx session -> HTTP 409 "already in
        # flight". (Two live tasks on one session also raced on self.turn /)
        # self.history.)
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=120)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001
                    pass
                self._drop_dangling_tool_calls()

    def _drop_dangling_tool_calls(self) -> None:
        """After a hard cancel, a trailing assistant message may carry
        tool_calls without their tool results — invalid for the next request.
        Drop the dangling assistant (and anything after it)."""
        h = self.history
        i = len(h) - 1
        while i >= 0:
            m = h[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                answers = {x.get("tool_call_id") for x in h[i + 1:]
                           if x.get("role") == "tool"}
                if {tc["id"] for tc in m["tool_calls"]} - answers:
                    del h[i:]
                return
            if m.get("role") in ("user", "system") or (
                    m.get("role") == "assistant"
                    and not m.get("tool_calls")):
                return
            i -= 1

    async def resume(self) -> None:
        if self.state != "stopped":
            return
        # Belt and braces: never start a run while an old task is still alive
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except BaseException:  # noqa: BLE001
                pass
        self._stop.clear()
        self._set_state("running")
        self.last_error = None
        self._task = asyncio.create_task(self._run())
        await self.events.publish("run_resumed", run_id=self.run_id)

    # ------------------------------------------------------------ SetSettings
    async def set_settings(self, patch: dict) -> Settings:
        """Apply non-zero/non-empty fields; validate on a COPY; settings_changed
        event. Mutating in place then validating would leave a corrupted
        Settings behind when validation fails (99999999 stays assigned)."""
        import dataclasses
        cand = dataclasses.replace(self.settings)
        if patch.get("max_context_tokens"):
            cand.max_context_tokens = int(patch["max_context_tokens"])
        if patch.get("max_output_tokens"):
            cand.max_output_tokens = int(patch["max_output_tokens"])
        if "thinking_enabled" in patch and patch["thinking_enabled"] is not None:
            cand.thinking_enabled = bool(patch["thinking_enabled"])
        if patch.get("thinking_effort") is not None:
            cand.thinking_effort = patch["thinking_effort"]
        if patch.get("sampling_temperature_x100"):
            cand.temperature = patch["sampling_temperature_x100"] / 100.0
        if patch.get("sampling_top_p_x100"):
            cand.top_p = patch["sampling_top_p_x100"] / 100.0
        if patch.get("sampling_top_k"):
            cand.top_k = int(patch["sampling_top_k"])
        cand.validate()          # raises -> nothing is applied
        self.settings = cand
        self.client.s = cand
        self.store.record({"type": "settings", "settings": cand.to_dict()})
        await self.events.publish("settings_changed", settings=cand.to_dict())
        return cand

    # --------------------------------------------------------------- Compact
    async def compact_request(self, keep_last_messages: int = 0) -> dict:
        keep = keep_last_messages if keep_last_messages > 0 else 10
        if self.state == "running":
            await self._turn_active.wait()          # wait for the current turn
        async with self._compact_in_flight:
            new_history, info = await compact(
                self.history, self.client, self.settings,
                keep_last_messages=keep, reason="manual")
            if info.get("ok"):
                self.history = new_history
                self.store.record_history(new_history)
            await self.events.publish("compact_done", **info)
            return info

    # ---------------------------------------------------- New session
    def _fresh_session(self) -> dict:
        self.session_id = str(uuid.uuid4())
        self.client.new_session_id()
        self.system_prompt = build_system_prompt()
        self.history = [{"role": "system", "content": self.system_prompt}]
        self.run_id = None
        self.turn = 0
        self.state = "idle"
        self.last_error = None
        self.last_prompt_tokens = 0
        self.last_usage = {}
        self._loaded = None
        self.store.reset()
        self.store.record({"type": "meta", "session_id": self.session_id,
                           "mtplx_session_id": self.client.session_id,
                           "model": self.settings.model,
                           "created": int(time.time() * 1000),
                           "settings": self.settings.to_dict()})
        self.store.record_message(self.history[0])
        return {"session_id": self.session_id}

    async def _archive_current(self) -> None:
        """Keep the current session in the archive (nothing to archive when
        only the system prompt is in history)."""
        if len(self.history) <= 1:
            return
        await self.store.flush()
        self.store.archive(self.session_id)
        # the archived session leaves the stage: drop its RAM KV now (SSD
        # snapshots stay; load_session() restores from them)
        try:
            await self.client.admin_clear_session(self.client.session_id)
        except Exception:
            pass

    async def new_session(self) -> dict:
        if self.state == "running":
            raise ValueError("run in progress — stop it first")
        await self._archive_current()
        r = self._fresh_session()
        await self.store.flush()      # make the fresh file visible to listers
        await self.events.publish("session_reset",
                                  session_id=self.session_id, messages=1)
        return r

    # ------------------------------------------------ Session management
    async def list_sessions(self) -> dict:
        return {"sessions": self.store.list_sessions(self.session_id)}

    async def load_session(self, session_id: str) -> dict:
        if not session_id:
            raise ValueError("session_id is empty")
        if self.state == "running":
            raise ValueError("a run is active or paused; stop it first")
        if session_id == self.session_id:
            return {"session_id": self.session_id,
                    "state": self.state, "loaded": False}
        await self._archive_current()
        data = self.store.restore_from(session_id)
        if data is None:
            raise ValueError(f"session {session_id} not found")
        self._apply_loaded(data)
        await self.events.publish("session_loaded",
                                  path=self.store.loaded_from or "",
                                  messages=len(self.history),
                                  state=self.state)
        return {"session_id": self.session_id, "state": self.state,
                "loaded": True}

    async def delete_session(self, session_id: str) -> dict:
        if not session_id:
            raise ValueError("session_id is empty")
        if session_id == self.session_id:
            if self.state == "running":
                raise ValueError("run in progress — stop it first")
            # also remove the archived copy (left behind when this session
            # was loaded back from the archive) or it would resurface
            self.store.delete_archive(session_id)
            try:
                await self.client.admin_clear_session(self.client.session_id)
            except Exception:
                pass
            await self.store.flush()
            r = self._fresh_session()
            await self.store.flush()
            await self.events.publish("session_reset",
                                      session_id=self.session_id, messages=1)
            return {"ok": True, "deleted": session_id, **r}
        data = self.store._replay(self.store.archive_path(session_id))
        if data is None:
            raise ValueError(f"session {session_id} not found")
        ok = self.store.delete_archive(session_id)
        if not ok:
            raise ValueError(f"session {session_id} not found")
        # drop the RAM KV that id may still hold (normally cleared at
        # archive time; belt and braces for pre-fix sessions)
        mid = (data.get("meta") or {}).get("mtplx_session_id")
        if mid and mid != self.client.session_id:
            try:
                await self.client.admin_clear_session(mid)
            except Exception:
                pass
        return {"ok": True, "deleted": session_id,
                "session_id": self.session_id}

    # ------------------------------------------------------- internal: compact
    def _needs_compact(self) -> bool:
        return (self.last_prompt_tokens + self.settings.max_output_tokens
                > self.settings.max_context_tokens)

    async def _auto_compact(self, keep: int = 10) -> bool:
        """Auto compact at the token boundary. Iterative: if the result is still
        over the boundary, compact again with a smaller keep (max 4 rounds)."""
        for round_ in range(4):
            await self.events.publish("compact_started",
                                      reason="auto",
                                      prompt_tokens=self.last_prompt_tokens,
                                      boundary=self.settings.max_context_tokens)
            async with self._compact_in_flight:
                new_history, info = await compact(
                    self.history, self.client, self.settings,
                    keep_last_messages=keep, reason="auto")
            if not info.get("ok"):
                await self.events.publish("compact_done", **info)
                return False
            self.history = new_history
            self.store.record_history(new_history)
            await self.events.publish("compact_done", **info)
            if not self._estimate_over_boundary(new_history):
                return True
            keep = max(4, keep // 2)
        return not self._estimate_over_boundary(self.history)

    def _estimate_over_boundary(self, history: list[dict]) -> bool:
        from .compaction import _token_estimate
        return (_token_estimate(history) + self.settings.max_output_tokens
                > self.settings.max_context_tokens)

    # -------------------------------------------------------------- run loop
    async def _run(self) -> None:
        run_id = self.run_id

        def finish(st: str) -> None:
            # If a newer run started while we were draining, our state writes
            # are stale — the new run owns the state machine from here on.
            if self.run_id == run_id:
                self._set_state(st)

        try:
            while True:
                if self._stop.is_set():
                    break
                # compaction gate: check before every LLM call (docs/COMPACT.md)
                if self._needs_compact():
                    if not await self._auto_compact(keep=10):
                        # last resort: force-compact the last 4 messages
                        if not await self._auto_compact(keep=4):
                            finish("error")
                            self.last_error = "CONTEXT_OVERFLOW: compaction " \
                                              "could not fit context window"
                            await self.events.publish(
                                "error", run_id=run_id,
                                code="CONTEXT_OVERFLOW",
                                message=self.last_error)
                            break
                # NB: _stop is NOT cleared here — it stays set for the whole
                # run and is only cleared when a new run starts (ask/resume/drain).
                # Clearing mid-loop would eat a Stop pressed during compaction.
                self.turn += 1
                self._turn_active.set()
                await self.events.publish("turn_started", run_id=run_id,
                                          turn=self.turn)
                try:
                    outcome = await self._llm_turn()
                finally:
                    self._turn_active.clear()
                await self.events.publish("turn_finished", run_id=run_id,
                                          turn=self.turn, reason=outcome)
                if outcome == "stopped":
                    finish("stopped")
                    break
                if outcome == "tool_calls":
                    # steering: user messages queued mid-run apply before next LLM call
                    while self.queue and not self._stop.is_set():
                        msg = self.queue.pop(0)
                        m = {"role": "user", "content": msg}
                        self.history.append(m)
                        self.store.record_message(m)
                    continue
                if outcome == "length":
                    m = {"role": "user", "content":
                        "Your previous response was cut off at max_tokens. "
                        "Continue from exactly where you stopped."}
                    self.history.append(m)
                    self.store.record_message(m)
                    continue
                if outcome == "retry":   # context overflow was compacted away
                    continue
                done = False
                if outcome == "stop":
                    finish("done")
                    done = True
                elif outcome == "stopped":
                    # already handled by the "stopped" branch above? no —
                    # "stopped" breaks earlier; kept for clarity
                    finish("stopped")
                    done = True
                elif outcome == "error":
                    done = True
                if done:
                    # drain remaining queued prompts into a fresh run
                    if self.queue and self.state in ("done", "stopped"):
                        next_prompt = self.queue.pop(0)
                        m = {"role": "user", "content": next_prompt}
                        self.history.append(m)
                        self.store.record_message(m)
                        self.run_id = str(uuid.uuid4())
                        run_id = self.run_id   # keep the finish() closure fresh
                        self._set_state("running")
                        self.store.record_run(self.run_id, "running", 0, "")
                        self._stop.clear()
                        await self.events.publish("run_resumed",
                                                 run_id=self.run_id)
                        continue
                    break
                    await self.events.publish("run_resumed", run_id=self.run_id)
                    continue
                break
        except Exception as e:  # noqa: BLE001 — last-resort guard for the run task
            finish("error")
            self.last_error = f"internal error: {e}"
            await self.events.publish("error", run_id=run_id, code="INTERNAL",
                                      message=self.last_error)
        finally:
            if self.run_id == run_id:
                await self.events.publish("run_done", run_id=run_id,
                                          state=self.state, turn=self.turn,
                                          error=self.last_error or "")
                # run record = durable marker that the run finished (its
                # absence on replay means the run was torn by a crash)
                self.store.record_run(run_id, self.state, self.turn,
                                      self.last_error or "")
                await self.store.flush()

    # ------------------------------------------------------------ subagents
    async def _release_main_session(self) -> None:
        """Pause the main session: wait until it is not in flight on mtplx,
        then clear its KV cache so the RAM serves the subagent instead.
        The main session resumes on the next LLM call (same session id)."""
        import sys
        client = self.client
        deadline = time.time() + subagents.RELEASE_WAIT_S
        while time.time() < deadline:
            info = await client.admin_session(client.session_id)
            if info is None or not info.get("in_flight"):
                break
            await asyncio.sleep(0.5)
        ok = await client.admin_clear_session(client.session_id)
        print(f"[harness] main session "
              f"{'released' if ok else 'release FAILED'} "
              f"(mtplx session {client.session_id[:8]})",
              file=sys.stderr, flush=True)
        await self.events.publish("main_paused", run_id=self.run_id,
                                  released=ok)

    async def _run_subagent(self, args: dict, tool_call_id: str) -> str:
        """Run one subagent for a task tool call; returns its report."""
        return await subagents.dispatch(self.root, self.settings, self.events,
                                        self._stop, args,
                                        self._active_subagents,
                                        tool_call_id=tool_call_id)

    # ------------------------------------------------------------ llm turn
    async def _llm_turn(self) -> str:
        """One LLM call + tool execution. Returns: stop | tool_calls | length |
        retry | error | stopped."""
        request = build_request(self.history, self.settings, TOOL_SCHEMAS)
        thinking = ""
        text = ""
        tool_calls: dict[int, dict] = {}
        announced: set[int] = set()
        finish_reason: str | None = None
        usage: dict | None = None

        def merge_tool(delta: dict) -> None:
            i = delta.get("index", 0)
            e = tool_calls.setdefault(i, {"id": None, "name": None, "args": ""})
            e["id"] = delta.get("id") or e["id"]
            if delta.get("name"):
                e["name"] = delta["name"]
            if delta.get("arguments"):
                e["args"] += delta["arguments"]
            if e["name"] and i not in announced:
                announced.add(i)
                asyncio.get_running_loop().create_task(
                    self.events.publish("tool_start", run_id=self.run_id,
                                        turn=self.turn, tool=e["name"],
                                        tool_call_id=e["id"],
                                        args_preview=e["args"][:500]))

        try:
            async for ev in self.client.stream_with_retry(request):
                if self._stop.is_set():
                    break
                k = ev["kind"]
                if k == "thinking":
                    thinking += ev["text"]
                    await self.events.publish("thinking_delta",
                                              run_id=self.run_id, turn=self.turn,
                                              text=ev["text"])
                elif k == "text":
                    text += ev["text"]
                    await self.events.publish("text_delta",
                                              run_id=self.run_id, turn=self.turn,
                                              text=ev["text"])
                elif k == "tool":
                    merge_tool(ev)
                elif k == "finish":
                    if ev.get("usage"):
                        usage = ev["usage"]
                    if ev.get("reason"):
                        finish_reason = ev["reason"]
        except ContextOverflowError as e:
            # P > W: force-compact the last 4 messages and retry once
            self.last_prompt_tokens = self.settings.max_context_tokens
            await self.events.publish("error", run_id=self.run_id, turn=self.turn,
                                      code="CONTEXT_OVERFLOW",
                                      message=str(e)[:300],
                                      detail="forcing compact(keep=4) and retrying")
            ok = await self._auto_compact(keep=4)
            if not ok:
                # the compact call itself may fail (e.g. GPU OOM: the engine
                # sheds its caches and stays up). One delayed retry usually
                # lands in the freed memory.
                await asyncio.sleep(3.0)
                ok = await self._auto_compact(keep=4)
            if ok:
                return "retry"          # run loop will call the LLM again
            self.state = "error"
            self.last_error = "CONTEXT_OVERFLOW"
            return "error"
        except LLMError as e:
            self.state = "error"
            self.last_error = str(e)
            await self.events.publish("error", run_id=self.run_id, turn=self.turn,
                                      code="LLM_ERROR", message=str(e)[:500])
            return "error"

        # partial content from a torn stream is preserved in history.
        # NB: tool_calls are deliberately NOT appended — unexecuted calls are
        # dropped (docs: an unexecuted tool_call is not executed), and a
        # dangling assistant tool_call without its tool result would make the
        # next request invalid. The model re-decides on resume/next run.
        if self._stop.is_set():
            if text or thinking:
                msg: dict = {"role": "assistant", "content": text}
                if thinking:
                    msg["reasoning_content"] = thinking
                self.history.append(msg)
                self.store.record_message(msg)
            return "stopped"

        # usage bookkeeping
        if usage:
            self.last_usage = usage
            self.last_prompt_tokens = usage.get("prompt_tokens", 0)
            await self.events.publish("usage", run_id=self.run_id, turn=self.turn,
                                      usage=usage)

        self._append_assistant(thinking, text, tool_calls)

        if finish_reason == "tool_calls" and tool_calls:
            has_task = any(tc["name"] == "task" for tc in tool_calls.values())
            if has_task:
                await self._release_main_session()
            for i in sorted(tool_calls):
                tc = tool_calls[i]
                args_raw = tc.get("args") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
                if tc["name"] == "task":
                    result = await self._run_subagent(args, tc["id"] or "")
                else:
                    result = await execute_tool(self.root, tc["name"], args)
                tm = {
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": result}
                self.history.append(tm)
                self.store.record_message(tm)
                preview = result[:500]
                is_error = result.startswith("error:")
                await self.events.publish("tool_end", run_id=self.run_id,
                                          turn=self.turn, tool=tc["name"],
                                          tool_call_id=tc["id"],
                                          ok=not is_error,
                                          result_preview=preview)
            return "tool_calls"
        if finish_reason == "length":
            return "length"
        return "stop"

    def _append_assistant(self, thinking: str, text: str,
                          tool_calls: dict[int, dict]) -> None:
        msg: dict = {"role": "assistant", "content": text}
        if thinking:
            msg["reasoning_content"] = thinking   # re-sent: --preserve-thinking auto
        if tool_calls:
            msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["args"] or "{}"}}
                for _, tc in sorted(tool_calls.items())]
        if tool_calls:
            msg["content"] = text  # may be ""
        self.history.append(msg)
        self.store.record_message(msg)

    # -------------------------------------------------------------- Status
    def get_messages(self, last: int = 0) -> list[dict]:
        h = self.history
        return list(h) if last <= 0 else list(h[-last:])

    async def status(self) -> dict:
        return {
            "settings": self.settings.to_dict(),
            "run": {"run_id": self.run_id or "", "state": self.state,
                    "turn": self.turn, "last_error": self.last_error or ""},
            "session_id": self.session_id,
            "history_messages": len(self.history),
            "prompt_tokens_last": self.last_prompt_tokens,
            "queue_depth": len(self.queue),
            "active_subagents": list(self._active_subagents),
            "started_at_unix_ms": self.started_at_ms,
        }

    async def aclose(self) -> None:
        if self._task and not self._task.done():
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, Exception):
                pass
        try:
            await self.client.admin_clear_session(self.client.session_id)
        except Exception:
            pass
        await self.store.close()
        await self.client.close()