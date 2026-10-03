"""Context compaction (docs/COMPACT.md).

Auto: fires when last usage.prompt_tokens + max_output_tokens > max_context_tokens.
Manual: via gRPC Compact.

Method: a rolling state brief. The brief is a structured user message
(Task / Done / State / Next). On each compaction the previous brief is NOT
re-summarized — it is passed to the summarizer as-is and updated with what
happened since, so information survives across many compactions instead of
being repeatedly compressed. Originals stay in the session JSONL; the
working history becomes [system, brief, ...kept tail].
"""
from __future__ import annotations

import json

from .llm import LLMClient, LLMError

SUMMARY_SYSTEM = (
    "You maintain the rolling state brief of a coding agent. You are given "
    "the previous brief (or none) and the conversation since it was written. "
    "Return the updated brief with exactly these sections, as short factual "
    "bullets, in the language the user speaks: "
    "## Task — the user's goal, 1-3 sentences (change only if the user "
    "changed it) "
    "## Done — decisions, files changed, commands run, errors fixed (keep "
    "prior items unless obsolete; append new ones) "
    "## State — where the work stands now "
    "## Next — remaining steps. "
    "Facts only: file paths, function names, exact commands, error messages "
    "that matter for continuing. No filler, no preamble, no opinions, no "
    "replay of tool chatter. If the task is done, say so in State and leave "
    "Next empty."
)

COMPACT_NOTE = "[Context compacted — rolling brief of the earlier conversation]\n"


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


def _token_estimate(history: list[dict]) -> int:
    return len(json.dumps(history, ensure_ascii=False)) // 4


def _is_brief(m: dict) -> bool:
    # matches the current note and the pre-rolling one in old sessions
    return (m.get("role") == "user"
            and (m.get("content") or "").startswith("[Context compacted"))


def _split(history: list[dict], brief_idx: int | None,
           keep_last_messages: int) -> tuple[list[dict], list[dict]]:
    """Split the non-system messages into (to_summarize, kept).

    Everything before the rolling brief is already gone (it was replaced by
    the brief), so to_summarize starts right after the brief. The cut never
    breaks an assistant(with tool_calls) + its tool-result group.
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


async def compact(history: list[dict], client: LLMClient, settings,
                  keep_last_messages: int = 10,
                  reason: str = "manual") -> tuple[list[dict], dict]:
    """Returns (new_history, info)."""
    before_msgs = len(history)
    before_tokens = _token_estimate(history)
    rest = [m for m in history if m.get("role") != "system"]
    brief_idx = next((i for i, m in enumerate(rest) if _is_brief(m)), None)
    prev_brief = ""
    if brief_idx is not None:
        prev_brief = rest[brief_idx]["content"].split("\n", 1)[1].strip()
    to_summarize, kept = _split(history, brief_idx, keep_last_messages)
    if not to_summarize:
        return history, {"ok": False, "reason": "nothing new since the last brief",
                         "tokens_before": before_tokens, "tokens_after": before_tokens}
    transcript = _flat_transcript(to_summarize)[:120_000]  # ~30K tok cap
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content":
            f"Previous brief:\n{prev_brief or '(none — first compaction)'}\n\n"
            f"Conversation since the last brief ({len(to_summarize)} messages, "
            f"reason: {reason}):\n\n{transcript}"},
    ]
    try:
        summary = await client.complete(
            messages, max_tokens=3000, enable_thinking=False)
    except LLMError as e:      # incl. ContextOverflowError, engine refusals
        return history, {"ok": False, "reason": f"llm error: {e}",
                         "tokens_before": before_tokens,
                         "tokens_after": before_tokens}
    if not summary.strip():
        return history, {"ok": False, "reason": "empty summary",
                         "tokens_before": before_tokens,
                         "tokens_after": before_tokens}
    new_history = ([m for m in history if m.get("role") == "system"] +
                   [{"role": "user",
                     "content": COMPACT_NOTE + summary.strip()}] + kept)
    after_tokens = _token_estimate(new_history)
    info = {"ok": True, "reason": reason,
            "tokens_before": before_tokens, "tokens_after": after_tokens,
            "messages_before": before_msgs, "messages_after": len(new_history),
            "summary_preview": summary.strip()[:200]}
    return new_history, info