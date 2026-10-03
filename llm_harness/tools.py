"""Agent tools: read / write / edit / bash / web_search / browser_fetch. All prompts and descriptions in English."""
from __future__ import annotations

import asyncio
import os

from .browserfetch import tool_browser_fetch
from .envinfo import effective_path
from .websearch import tool_web_search

MAX_LINES = 2000
MAX_BYTES = 50_000
BASH_DEFAULT_TIMEOUT = 120
BASH_MAX_TIMEOUT = 600

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Read a file from the workspace and return its numbered lines "
                "as 'NNNN| content'. Directories are listed. Use offset/limit "
                "(1-based line numbers) to read a slice of a large file."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "File or directory path (relative to workspace root)."},
                    "offset": {"type": "integer",
                               "description": "First line to return (1-based). Default 1."},
                    "limit": {"type": "integer",
                              "description": "Maximum number of lines to return. Default 2000."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": (
                "Write (create or fully overwrite) a file with the given content. "
                "Parent directories are created. Use edit for targeted changes."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to workspace root)."},
                    "content": {"type": "string", "description": "Full new file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": (
                "Replace one exact occurrence of oldText with newText in a file. "
                "oldText must match the file content exactly (whitespace included) "
                "and be unique in the file; include enough surrounding context "
                "to make it unique. Read the file first to get the exact text."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to workspace root)."},
                    "oldText": {"type": "string", "description": "Exact text to find (must be unique)."},
                    "newText": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "oldText", "newText"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command in the workspace root and return its "
                "combined stdout+stderr. Use for builds, tests, git, installs. "
                "Commands that would hang are killed at the timeout."),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    "timeout": {"type": "integer",
                               "description": f"Timeout in seconds (default {BASH_DEFAULT_TIMEOUT}, max {BASH_MAX_TIMEOUT})."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web and return ranked results with titles, URLs and "
                "snippets. Uses Brave Search, falling back to Google Custom "
                "Search when the Brave quota is exhausted. Use for finding "
                "current facts, documentation, or locating sources; fetch a "
                "promising result URL with bash curl to read it in full."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "count": {"type": "integer",
                             "description": "Number of results (1-10, default 8)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fetch",
            "description": (
                "Open a URL in the user's real logged-in Chrome browser "
                "(attached over Chrome DevTools Protocol, opens a fresh tab "
                "and closes it after reading), wait for the page to load and "
                "return the rendered page text (title + innerText). Use for "
                "JavaScript-heavy or login-walled pages that plain HTTP fetch "
                "cannot read (e.g. reddit.com, x.com). Takes a few seconds "
                "per call."),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "Absolute http(s) URL to open and read."},
                    "wait": {"type": "number",
                             "description": "Seconds to wait for page load (1-30, default 10)."},
                    "js": {"type": "string",
                           "description": "Optional JavaScript expression to evaluate on the page instead of the default title + innerText."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": (
                "Run a subagent on one self-contained piece of work and get "
                "its final report. Use it when a part of the job is research "
                "(web or codebase), file search, or any step whose "
                "intermediate output would flood your context. The main "
                "session is paused while the subagent runs; you receive ONLY "
                "the report, never the subagent's steps, so the instruction "
                "must be fully self-contained: exact goal, files/URLs to use, "
                "and the report format you need. Write the instruction in "
                "English. In TASK MODE the pipeline roles (recon, deep, "
                "analyst, orchestrator, builder, reviewer) run in sequence - "
                "see the task mode rules."),
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["explore", "research", "builder", "recon",
                                 "deep", "analyst", "orchestrator",
                                 "reviewer"],
                        "description": (
                            "explore: read-only codebase search (read, bash); "
                            "research: web + codebase (adds web_search, "
                            "browser_fetch); builder: all tools, may modify "
                            "files. Pipeline roles: recon (locate relevant "
                            "code), deep (how it works), analyst (plan), "
                            "orchestrator (split plan into specs), reviewer "
                            "(review the result)."),
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "Self-contained instruction for the subagent "
                            "(English)."),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional extra context for the subagent: "
                            "snippets, earlier findings, constraints."),
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": (
                            "Subagent turn budget (default 20, max 40)."),
                    },
                },
                "required": ["role", "task"],
            },
        },
    },
]


def _resolve(root: str, path: str) -> str:
    root_abs = os.path.abspath(root)
    p = os.path.abspath(os.path.join(root_abs, path))
    if p != root_abs and not p.startswith(root_abs + os.sep):
        raise ValueError(f"path escapes workspace root: {path}")
    return p


def _cap(text: str, keep_tail: bool = False) -> str:
    if len(text) <= MAX_BYTES:
        return text
    if keep_tail:
        return text[-MAX_BYTES:] + f"\n...[truncated, {len(text) - MAX_BYTES} bytes dropped from the beginning]"
    return text[:MAX_BYTES] + f"\n...[truncated, {len(text) - MAX_BYTES} bytes dropped]"


# -- read ---------------------------------------------------------------------
def tool_read(root: str, args: dict) -> str:
    try:
        p = _resolve(root, args["path"])
    except ValueError as e:
        return f"error: {e}"
    if os.path.isdir(p):
        entries = sorted(os.listdir(p))[:200]
        listing = "\n".join(
            e + ("/" if os.path.isdir(os.path.join(p, e)) else "") for e in entries)
        return f"Directory listing of {args['path']}:\n{listing or '(empty)'}"
    if not os.path.isfile(p):
        return f"error: file not found: {args['path']}"
    try:
        raw = open(p, "r", encoding="utf-8", errors="replace").read()
    except OSError as e:
        return f"error: {e}"
    lines = raw.replace("\r\n", "\n").split("\n")
    total = len(lines)
    offset = max(int(args.get("offset") or 1), 1)
    limit = int(args.get("limit") or MAX_LINES)
    limit = min(max(limit, 1), MAX_LINES)
    window = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{i:4d}| {line}" for i, line in
                     enumerate(window, start=offset))
    if len(body) > MAX_BYTES:
        body = body[:MAX_BYTES]
        while body and body[-1] != "\n":
            body = body[:-1]
        body += f"\n...[truncated at {MAX_BYTES} bytes]"
    suffix = ""
    if offset > 1 or offset - 1 + len(window) < total:
        suffix = f"\n({total} lines total)"
    return body + suffix


# -- write --------------------------------------------------------------------
def tool_write(root: str, args: dict) -> str:
    try:
        p = _resolve(root, args["path"])
    except ValueError as e:
        return f"error: {e}"
    content = args.get("content", "")
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"error: {e}"
    return f"Wrote {len(content.encode('utf-8'))} bytes to {args['path']}"


# -- edit ---------------------------------------------------------------------
def tool_edit(root: str, args: dict) -> str:
    try:
        p = _resolve(root, args["path"])
    except ValueError as e:
        return f"error: {e}"
    old, new = args.get("oldText", ""), args.get("newText", "")
    if old == "":
        return "error: oldText is empty"
    if not os.path.isfile(p):
        return f"error: file not found: {args['path']}"
    raw = open(p, "r", encoding="utf-8", errors="replace").read()
    trailing = "\n" if raw.endswith("\n") else ""
    body = raw[:-1] if trailing else raw
    body = body.replace("\r\n", "\n")
    count = body.count(old)
    if count == 0:
        snippet = old[:60].replace("\n", "\\n")
        return (f"error: oldText not found (must match exactly, incl. whitespace). "
                f"Looked for: {snippet!r}...")
    if count > 1:
        return (f"error: oldText matches {count} places; it must be unique. "
                f"Add surrounding context to disambiguate.")
    body = body.replace(old, new, 1)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(body + trailing)
    except OSError as e:
        return f"error: {e}"
    return f"Edited {args['path']} (1 replacement)"


# -- bash ---------------------------------------------------------------------
async def tool_bash(root: str, args: dict) -> str:
    command = args.get("command", "")
    timeout = min(int(args.get("timeout") or BASH_DEFAULT_TIMEOUT), BASH_MAX_TIMEOUT)
    try:
        env = dict(os.environ)
        env["PATH"] = effective_path()
        proc = await asyncio.create_subprocess_shell(
            command, cwd=root, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"error: command timed out after {timeout}s and was killed"
        text = out.decode("utf-8", "replace")
    except OSError as e:
        return f"error: {e}"
    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        text = "\n".join(lines[-MAX_LINES:]) + \
            f"\n...[truncated, {len(lines) - MAX_LINES} lines dropped from the beginning]"
    text = _cap(text)
    rc = proc.returncode or 0
    prefix = "" if rc == 0 else f"exit code: {rc}\n"
    return prefix + (text or "(no output)")


TOOL_IMPLS = {
    "read": lambda root, args: asyncio.to_thread(tool_read, root, args),
    "write": lambda root, args: asyncio.to_thread(tool_write, root, args),
    "edit": lambda root, args: asyncio.to_thread(tool_edit, root, args),
    "bash": tool_bash,
    "web_search": lambda root, args: asyncio.to_thread(tool_web_search, root, args),
    "browser_fetch": lambda root, args: asyncio.to_thread(tool_browser_fetch, root, args),
}


async def execute_tool(root: str, name: str, args: dict) -> str:
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return f"error: unknown tool: {name}"
    return await impl(root, args)
