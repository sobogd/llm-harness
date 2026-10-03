"""Host environment snapshot for the system prompt.

Built once at harness start: what is installed (tools + versions),
what is running (listening services, top processes), and the PATH
tool subprocesses actually use. Injected into the system prompt so
the agent knows the machine without probing for it.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

EXTRA_PATH_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin")

TOOLS: tuple[tuple[str, str], ...] = (
    ("git", "--version"),
    ("gh", "--version"),
    ("brew", "--version"),
    ("flutter", "--version"),
    ("dart", "--version"),
    ("node", "--version"),
    ("npm", "--version"),
    ("python3", "--version"),
    ("pip3", "--version"),
    ("java", "-version"),
    ("go", "version"),
    ("rustc", "--version"),
    ("docker", "--version"),
    ("sqlite3", "--version"),
    ("jq", "--version"),
)

MAX_REPORT_CHARS = 3500
PROBE_TIMEOUT = 10.0


def effective_path() -> str:
    """PATH that tool subprocesses get: homebrew first (like a user
    shell), then whatever the harness inherited."""
    parts = [p for p in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep) if p]
    return os.pathsep.join(list(reversed(EXTRA_PATH_DIRS)) +
                           [p for p in parts if p not in EXTRA_PATH_DIRS])


def _run(cmd: list[str], path: str | None = None) -> str | None:
    path = path or effective_path()
    env = {"PATH": path, "HOME": os.path.expanduser("~")}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=PROBE_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    text = (out.stdout + out.stderr).strip()
    return text or None


def _installed(path: str) -> list[str]:
    def probe(tool: tuple[str, str]) -> str | None:
        name, flag = tool
        p = shutil.which(name, path=path)
        if not p:
            return None
        first = (_run([p, flag], path) or "").splitlines()
        ver = first[0].strip() if first else ""
        line = f"- {name} {p}"
        if ver:
            line += f" ({ver})"
        return line

    with ThreadPoolExecutor(max_workers=8) as ex:
        found = [r for r in ex.map(probe, TOOLS) if r]
    return found or ["- none of the tracked tools found"]


def _pid_names() -> dict[str, str]:
    text = _run(["ps", "-axo", "pid,comm"])
    names: dict[str, str] = {}
    if text:
        for ln in text.splitlines()[1:]:
            parts = ln.split(None, 1)
            if len(parts) == 2:
                names[parts[0]] = parts[1].strip().rsplit("/", 1)[-1]
    return names


def _listening(path: str) -> list[str]:
    text = _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], path)
    if not text:
        return ["- (lsof unavailable)"]
    names = _pid_names()
    seen: dict[tuple[str, str], None] = {}
    for ln in text.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 9:
            continue
        addr = parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]
        cmd = names.get(parts[1], parts[0])
        seen.setdefault((cmd, addr), None)
    return [f"- {cmd} on {addr}" for cmd, addr in sorted(seen)] \
        or ["- (nothing listening)"]


def _processes() -> str:
    text = _run(["ps", "-axo", "comm="])
    if not text:
        return "- (unavailable)"
    counts = Counter(ln.strip().rsplit("/", 1)[-1]
                     for ln in text.splitlines() if ln.strip())
    top = ", ".join(f"{n} x{k}" for n, k in counts.most_common(10))
    return f"- {top}"


def environment_report() -> str:
    path = effective_path()
    lines = [
        "Environment snapshot (host, taken at harness start; refresh "
        "via bash if the machine has changed since):",
        f"- host: {platform.system()} {platform.release()} "
        f"({platform.machine()}) user {os.environ.get('USER', '?')} "
        f"{platform.node()}",
        f"- bash tool PATH: {path}",
        "Installed tools:",
        *_installed(path),
        "Listening services:",
        *_listening(path),
        "Running processes (top by count):",
        _processes(),
    ]
    report = "\n".join(lines)
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n...[snapshot truncated]"
    return report
