"""Context compaction (docs/COMPACT.md).

Auto: fires when last usage.prompt_tokens + max_output_tokens > max_context_tokens.
Manual: via gRPC Compact.

Method: an append-only segment chain inside a single brief message:
verbatim Task, verbatim user messages, one summary per compacted window
(C1: start -> compaction 1, C2: after compaction 1 -> compaction 2, ...),
plus a small State/Next tail. Each compaction summarizes only the new
window and appends it; older segments are never re-summarized, so the
task intent does not degrade across compactions. When the brief outgrows
BRIEF_BUDGET_TOKENS the oldest segments are merged into one condensed
Archive section (and, if still over budget, the accumulated user messages
are condensed too). Originals stay in the session JSONL; the working
history becomes [system, brief, ...kept tail].
"""
from __future__ import annotations

import json
import re

from .llm import LLMClient, LLMError

BRIEF_BUDGET_TOKENS = 30_000   # archive older segments beyond this
KEEP_VERBATIM_SEGMENTS = 3     # most recent segments never archived
NOTE_PREFIX = "[Context compacted"

SEGMENT_SYSTEM = (
    "You maintain the segment chain of a coding agent's context brief. You "
    "are given the task, the segments summarized so far, the current "
    "State/Next, and the new conversation segment that just ended. Return "
    "exactly these sections, factual bullets, in the language the user speaks: "
    "## Segment — what happened in the NEW segment only: decisions, files "
    "changed, commands run, errors fixed, key results. Under 400 words. Do "
    "not restate user messages verbatim (they are kept separately) and do "
    "not repeat material from earlier segments. "
    "## State — where the work stands now (previous State updated with the "
    "new segment). If the task is done, say so. "
    "## Next — remaining steps (empty if the task is done)."
)

ARCHIVE_SYSTEM = (
    "Condense the older segments of a coding agent brief into one short "
    "archive. Keep only what still matters for continuing the task: key "
    "decisions, files, fixed errors, milestones. Factual bullets, under 300 "
    "words, in the language the task is written. No section headers."
)

USERS_SYSTEM = (
    "Condense the accumulated user messages of a long coding agent session "
    "into short factual bullets: what the user asked, decided or corrected, "
    "in order. Under 300 words, in the language the user speaks. No section "
    "headers."
)


def _flat_transcript(messages: list[dict]) -> str:
    """Flatten messages into a transcript for the summarizer (no thinking tokens)."""
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if isinstance(content, list):  # tolerate multimodal parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        tcs = m.get("tool_calls") or []
        tc_desc = ""
        if tcs:
            names = [f"{t.get('function', {}).get('name', '?')}"
                     f"({t.get('function', {}).get('arguments', '')})" for t in tcs]
            tc_desc = " [tool calls: " + "; ".join(names) + "]"
        if role == "tool":
            snippet = content[:800]
            parts.append(f"[tool result] {snippet}")
        else:
            parts.append(f"[{role}] {content}{tc_desc}")
    return "\n".join(parts)


def _text_of(m: dict) -> str:
    content = m.get("content") or ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content.strip()


def _user_messages(messages: list[dict]) -> list[str]:
    """Verbatim user messages (capped), in order."""
    out = []
    for m in messages:
        if m.get("role") != "user":
            continue
        t = _text_of(m)
        if t:
            out.append(t[:4000])
    return out


def _token_estimate(history: list[dict]) -> int:
    return len(json.dumps(history, ensure_ascii=False)) // 4


def _is_brief(m: dict) -> bool:
    # matches the current note and the pre-chain rolling one in old sessions
    return (m.get("role") == "user"
            and (m.get("content") or "").startswith(NOTE_PREFIX))


def _split(history: list[dict], brief_idx: int | None,
           keep_last_messages: int) -> tuple[list[dict], list[dict]]:
    """Split the non-system messages into (to_summarize, kept).

    Everything before the brief is already condensed into it, so
    to_summarize starts right after the brief. The cut never breaks an
    assistant(with tool_calls) + its tool-result group.
    """
    rest = [m for m in history if m.get("role") != "system"]
    body = rest[brief_idx + 1:] if brief_idx is not None else rest
    keep = max(int(keep_last_messages), 2)
    if len(body) <= keep:
        return [], body
    cut = len(body) - keep
    while cut < len(body) and body[cut].get("role") == "tool":
        cut += 1
    if cut < 1:
        return [], body
    return body[:cut], body[cut:]


def _parse_sections(body: str) -> dict[str, str]:
    """Parse '## Header' sections into {header: text}."""
    secs: dict[str, str] = {}
    cur, buf = None, []
    for line in body.splitlines():
        if line.startswith("## "):
            if cur is not None:
                secs[cur] = "\n".join(buf).strip()
            cur, buf = line[3:].strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        secs[cur] = "\n".join(buf).strip()
    return secs


def _parse_brief(content: str) -> dict:
    """Parse the brief body into task/users/segments/state/next/archived.

    segments[0] is the Archive section when present. Also migrates the old
    rolling-brief format (Task/Done/State/Next): Done becomes segment C1.
    """
    body = content.split("\n", 1)[1] if "\n" in content else ""
    secs = _parse_sections(body)
    if not secs:
        return {"task": "", "users": "", "segments": [],
                "state": "", "next": "", "archived": False}
    segments = []
    if secs.get("Archive"):
        segments.append(secs["Archive"])
    segments += [secs[h] for h in sorted(
        (h for h in secs if re.fullmatch(r"C\d+", h)), key=lambda h: int(h[1:]))
        if secs[h]]
    if not segments and secs.get("Done"):
        segments = [secs["Done"]]   # pre-chain brief migration
    return {"task": secs.get("Task", "").strip(),
            "users": secs.get("Users", "").strip(),
            "segments": segments,
            "state": secs.get("State", "").strip(),
            "next": secs.get("Next", "").strip(),
            "archived": bool(secs.get("Archive"))}


def _render_brief(parts: dict) -> dict:
    """Render the brief as a user message dict from parsed parts."""
    lines = [f"{NOTE_PREFIX} — {len(parts['segments'])} segment(s)]"]
    if parts["task"]:
        lines += ["## Task", parts["task"]]
    if parts["users"]:
        lines += ["## Users", parts["users"]]
    for i, s in enumerate(parts["segments"], 1):
        header = "Archive" if i == 1 and parts.get("archived") else f"C{i}"
        lines += [f"## {header}", s]
    if parts["state"]:
        lines += ["## State", parts["state"]]
    if parts["next"]:
        lines += ["## Next", parts["next"]]
    return {"role": "user", "content": "\n".join(lines)}


async def _condense(client: LLMClient, system: str, text: str) -> str | None:
    """One small LLM call condensing a text block. None on LLM error."""
    try:
        out = await client.complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": text}],
            max_tokens=1200, enable_thinking=False)
    except LLMError:
        return None
    return out.strip() or None


async def _archive_segments(client: LLMClient, task: str,
                           to_archive: list[str]) -> str | None:
    """Merge old segments into one condensed archive. None on LLM error."""
    return await _condense(client, ARCHIVE_SYSTEM,
                           f"Task:\n{task or '(unknown)'}\n\n"
                           f"Segments to condense:\n"
                           + "\n\n".join(to_archive))


async def compact(history: list[dict], client: LLMClient, settings,
                  keep_last_messages: int = 10,
                  reason: str = "manual") -> tuple[list[dict], dict]:
    """Returns (new_history, info)."""
    before_msgs = len(history)
    before_tokens = _token_estimate(history)
    rest = [m for m in history if m.get("role") != "system"]
    brief_idx = next((i for i, m in enumerate(rest) if _is_brief(m)), None)
    to_summarize, kept = _split(history, brief_idx, keep_last_messages)
    if not to_summarize:
        return history, {"ok": False, "reason": "nothing new since the last brief",
                         "tokens_before": before_tokens, "tokens_after": before_tokens}

    if brief_idx is not None:
        prev = _parse_brief(rest[brief_idx]["content"])
    else:
        prev = {"task": "", "users": "", "segments": [],
                "state": "", "next": "", "archived": False}
        first_user = _user_messages(rest)
        prev["task"] = first_user[0] if first_user else ""

    user_msgs = _user_messages(to_summarize)
    # the task itself lives in ## Task; don't duplicate it in ## Users
    if user_msgs and prev["task"] and user_msgs[0] == prev["task"]:
        user_msgs = user_msgs[1:]
    users = prev["users"]
    if user_msgs:
        users = (users + "\n\n" + "\n\n".join(user_msgs)).strip() if users \
            else "\n\n".join(user_msgs)

    transcript = _flat_transcript(to_summarize)[:120_000]  # ~30K tok cap
    prev_segments = prev["segments"]
    headers = (["Archive"] if prev["archived"] else []) + \
        [f"C{i}" for i in range(1, len(prev_segments) + 1)]
    prev_seg_text = "\n\n".join(
        f"[{h}]\n{s}" for h, s in zip(headers, prev_segments)) \
        if prev_segments else ""
    if len(prev_seg_text) > 20_000:   # most recent segments matter most
        prev_seg_text = prev_seg_text[-20_000:]
    messages = [
        {"role": "system", "content": SEGMENT_SYSTEM},
        {"role": "user", "content":
            f"Task:\n{prev['task'] or '(unknown)'}\n\n"
            f"Previous segments (most recent last):\n"
            f"{prev_seg_text or '(none — first segment)'}\n\n"
            f"Previous State:\n{prev['state'] or '(none)'}\n\n"
            f"Previous Next:\n{prev['next'] or '(none)'}\n\n"
            f"New segment transcript ({len(to_summarize)} messages, "
            f"reason: {reason}):\n\n{transcript}"},
    ]
    try:
        summary = await client.complete(
            messages, max_tokens=3000, enable_thinking=False)
    except LLMError as e:      # incl. ContextOverflowError, engine refusals
        return history, {"ok": False, "reason": f"llm error: {e}",
                         "tokens_before": before_tokens,
                         "tokens_after": before_tokens}
    out_secs = _parse_sections(summary.strip())
    segment = out_secs.get("Segment", "").strip()
    if not segment:
        return history, {"ok": False, "reason": "empty segment summary",
                         "tokens_before": before_tokens,
                         "tokens_after": before_tokens}

    parts = {"task": prev["task"], "users": users,
             "segments": prev_segments + [segment],
             "state": out_secs.get("State", "").strip() or prev["state"],
             "next": out_secs.get("Next", "").strip(),
             "archived": prev["archived"]}

    # budget cap: condense the oldest segments into one archive; if the brief
    # is still over budget, condense the accumulated user messages too
    if (_token_estimate([_render_brief(parts)]) > BRIEF_BUDGET_TOKENS
            and len(parts["segments"]) > 1):
        if len(parts["segments"]) > KEEP_VERBATIM_SEGMENTS:
            keep_tail = parts["segments"][-KEEP_VERBATIM_SEGMENTS:]
            to_archive = parts["segments"][:-len(keep_tail)]
        else:
            keep_tail = parts["segments"][-1:]
            to_archive = parts["segments"][:-1]
        archived = await _archive_segments(client, parts["task"], to_archive)
        if archived:
            parts["segments"] = [archived] + keep_tail
            parts["archived"] = True
    if (_token_estimate([_render_brief(parts)]) > BRIEF_BUDGET_TOKENS
            and parts["users"]):
        condensed = await _condense(client, USERS_SYSTEM, parts["users"])
        if condensed:
            parts["users"] = condensed

    new_history = ([m for m in history if m.get("role") == "system"] +
                   [_render_brief(parts)] + kept)

    after_tokens = _token_estimate(new_history)
    info = {"ok": True, "reason": reason,
            "tokens_before": before_tokens, "tokens_after": after_tokens,
            "messages_before": before_msgs, "messages_after": len(new_history),
            "segments": len(parts["segments"]),
            "summary_preview": segment[:200]}
    return new_history, info
