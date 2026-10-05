"""JSONL session persistence (phase 6).

The session file lives at ``<root>/.llm-harness/session.jsonl`` and is a
newline-delimited stream of records. Replay is idempotent:

  meta     {"type":"meta", ...}          session header (written once)
  message  {"type":"message","msg":M}    one history message, in order
  history  {"type":"history","messages":H} full snapshot (written after
                                    compaction; replaces the replayed tail)
  run      {"type":"run", ...}           informational: run finished
  settings {"type":"settings","settings":S} informational: settings changed

A crashed server loses at most the last ~0.5 s of unflushed records; a
restarted server replays the file and either resumes a torn run (state
"stopped") or continues the conversation from a fresh run (state "idle").
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

DIR_NAME = ".llm-harness"
FILE_NAME = "session.jsonl"
SESSIONS_DIR = "sessions"
FLUSH_INTERVAL_S = 0.5
PREVIEW_CHARS = 100


def _safe_id(session_id: str) -> str:
    """File-name-safe session id (archive files are <id>.jsonl)."""
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in session_id)
    return out[:64] or "session"


class SessionStore:
    """Append-only JSONL writer + replay loader.

    All ``record()`` calls come from the event-loop thread; a single
    background flusher writes them in order, so line order is preserved.
    """

    def __init__(self, root: str):
        self.dir = Path(root) / DIR_NAME
        self.path = self.dir / FILE_NAME
        self.sessions_dir = self.dir / SESSIONS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._pending: list[str] = []
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._stop_evt = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.written = 0            # lines flushed so far in this process
        self.loaded_from: str | None = None
        self.loaded_messages = 0

    # ---------------------------------------------------------------- write
    def _ensure_task(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._sync_mode = True
            return
        self._task = loop.create_task(self._flusher())

    def record(self, obj: dict) -> None:
        """Enqueue one record for durable write (sync; loop-thread only).
        Falls back to a direct synchronous write when no event loop is
        running (defensive: shutdown, tests)."""
        if self._stop_evt.is_set():
            return
        self._pending.append(json.dumps(obj, ensure_ascii=False))
        self._idle.clear()
        self._wake.set()
        self._ensure_task()
        if getattr(self, "_sync_mode", False):
            batch, self._pending = self._pending, []
            self._write(batch)
            self.written += len(batch)
            self._idle.set()

    def record_message(self, msg: dict) -> None:
        self.record({"type": "message", "msg": msg})

    def record_history(self, messages: list[dict]) -> None:
        self.record({"type": "history", "messages": messages})

    def record_run(self, run_id: str, state: str, turn: int,
                   error: str) -> None:
        self.record({"type": "run", "run_id": run_id, "state": state,
                     "turn": turn, "error": error,
                     "ts": int(time.time() * 1000)})

    async def flush(self) -> None:
        """Wait until everything enqueued so far is on disk."""
        self._ensure_task()
        await self._idle.wait()

    def reset(self) -> None:
        try:
            self.path.write_text("", encoding="utf-8")
        except OSError:
            pass
        self.written = 0

    async def _flusher(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=FLUSH_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._pending:
                batch, self._pending = self._pending, []
                await asyncio.to_thread(self._write, batch)
                self.written += len(batch)
            self._idle.set()
            if self._stop_evt.is_set() and not self._pending:
                return

    def _write(self, lines: list[str]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()

    async def close(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                pass
        await self.flush()

    # ----------------------------------------------------------------- load
    def _replay(self, path: Path) -> dict | None:
        """Replay a session JSONL file (no side effects on self).
        Returns None when the file is missing or empty.

        Result: {"meta": {...}|None, "history": [...], "last_run": {...}|None,
                 "settings": {...}|None, "name": str|None, "path": Path}
        """
        if not path.exists():
            return None
        meta = None
        history: list[dict] = []
        last_run = None
        settings = None
        name = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue          # torn tail write — skip
            t = rec.get("type")
            if t == "meta":
                meta = rec
            elif t == "message":
                history.append(rec["msg"])
            elif t == "history":
                history = list(rec.get("messages") or [])
            elif t == "run":
                last_run = rec
            elif t == "settings":
                settings = rec.get("settings")
            elif t == "rename":
                name = rec.get("name")
        if meta is None and not history:
            return None
        return {"meta": meta, "history": history, "last_run": last_run,
                "settings": settings, "name": name, "path": path}

    def load(self) -> dict | None:
        """Replay the active session file. Returns None for a fresh session."""
        data = self._replay(self.path)
        if data is None:
            return None
        self.loaded_from = str(self.path)
        self.loaded_messages = len(data["history"])
        return data

    # ------------------------------------------------------------ sessions
    def archive_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_safe_id(session_id)}.jsonl"

    def archive(self, session_id: str) -> Path | None:
        """Copy the active session file into the archive (idempotent).
        Call flush() first so the copy is complete. Returns the archive
        path, or None if there is nothing to archive."""
        if not self.path.exists():
            return None
        data = self._replay(self.path)
        if data is None:
            return None
        import shutil
        dst = self.archive_path(session_id)
        shutil.copyfile(self.path, dst)
        return dst

    def restore_from(self, session_id: str) -> dict | None:
        """Replace the active session file with the content of an archived
        session and mark it loaded. Returns the replay data (or None)."""
        data = self._replay(self.archive_path(session_id))
        if data is None:
            return None
        self._pending.clear()
        self._idle.set()
        self.path.write_text(
            self.archive_path(session_id).read_text(encoding="utf-8"),
            encoding="utf-8")
        self.written = 0
        self.loaded_from = str(self.path)
        self.loaded_messages = len(data["history"])
        return data

    def rename(self, session_id: str, name: str) -> bool:
        """Append a display-name record. The active session goes through the
        flusher (order-preserving); an archived one is appended directly to
        its file (single line). Returns False when the session is unknown."""
        rec = json.dumps({"type": "rename", "name": name}, ensure_ascii=False)
        if self.path.exists():
            meta = (self._replay(self.path) or {}).get("meta") or {}
            if meta.get("session_id") == session_id:
                self.record({"type": "rename", "name": name})
                return True
        p = self.archive_path(session_id)
        if not p.exists():
            return False
        with open(p, "a", encoding="utf-8") as f:
            f.write(rec + "\n")
        return True

    def delete_archive(self, session_id: str) -> bool:
        p = self.archive_path(session_id)
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return False

    def list_sessions(self, active_id: str) -> list[dict]:
        """All sessions (active + archived), newest activity first.

        Each entry: {"id", "preview", "name", "messages", "created_ms",
                     "updated_ms", "active"}
        """
        out: list[dict] = []

        def entry(path: Path, sid: str) -> None:
            data = self._replay(path)
            if data is None:
                return
            meta = data.get("meta") or {}
            history = data["history"]
            preview = ""
            for m in history:
                if m.get("role") == "user":
                    preview = (m.get("content") or "").replace("\n", " ").strip()
                    break
            out.append({
                "id": sid,
                "preview": preview[:PREVIEW_CHARS],
                "name": data.get("name") or "",
                "messages": len(history),
                "created_ms": int(meta.get("created") or 0),
                "updated_ms": int(path.stat().st_mtime * 1000),
                "active": sid == active_id,
            })

        if self.path.exists():
            meta = (self._replay(self.path) or {}).get("meta") or {}
            entry(self.path, meta.get("session_id") or active_id)
        for f in sorted(self.sessions_dir.glob("*.jsonl")):
            meta = (self._replay(f) or {}).get("meta") or {}
            sid = meta.get("session_id") or _safe_id(f.stem)
            if sid == active_id:
                continue      # loaded back from the archive: the active file
                              # is the source of truth — don't list it twice
            entry(f, sid)
        out.sort(key=lambda e: e["updated_ms"], reverse=True)
        return out