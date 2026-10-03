"""Repro: old sessions missing from ListSessions (run with .venv/bin/python)."""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_harness.config import Settings
from llm_harness.harness import Harness


async def drive(h, text):
    """Simulate a user message + assistant reply without a real LLM."""
    h.history.append({"role": "user", "content": text})
    h.store.record_message({"role": "user", "content": text})
    h.history.append({"role": "assistant", "content": "ok: " + text})
    h.store.record_message({"role": "assistant", "content": "ok: " + text})
    await h.store.flush()


async def main():
    root = tempfile.mkdtemp(prefix="lh-")
    print("root:", root)
    settings = Settings()
    settings.model = "test"
    h = Harness(root, settings)
    await h.startup()
    sid0 = h.session_id
    print("initial session:", sid0)

    # Conversation 1
    await drive(h, "hello one")

    # New session (archives #1)
    sid1 = (await h.new_session())["session_id"]
    print("session #1:", sid1)
    await drive(h, "hello two")

    # New session again (archives #2)
    sid2 = (await h.new_session())["session_id"]
    print("session #2:", sid2)
    await drive(h, "hello three")

    listing = (await h.list_sessions())["sessions"]
    print("\nLIST:")
    for s in listing:
        print(f"  active={s['active']} id={s['id'][:8]} msg={s['messages']} preview={s['preview']!r}")
    ids = {s["id"] for s in listing}
    expected = {sid0, sid1, sid2}
    missing = expected - ids
    print("expected:", {x[:8] for x in expected})
    print("MISSING:", [x[:8] for x in missing] if missing else "none")

    # file contents sanity
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            print("\nFILE", os.path.relpath(p, root))
            for line in open(p, encoding="utf-8").read().splitlines():
                print("   ", line[:150])

    await h.aclose()

    # Restart scenario: fresh harness on same root
    print("\n--- restart ---")
    h2 = Harness(root, Settings())
    await h2.startup()
    print("restored session:", h2.session_id)
    listing2 = (await h2.list_sessions())["sessions"]
    for s in listing2:
        print(f"  active={s['active']} id={s['id'][:8]} msg={s['messages']} preview={s['preview']!r}")

    # delete one, list again
    del_id = [s for s in listing2 if s["id"] == sid0][0]["id"]
    print("\ndelete", del_id[:8], "->", await h2.delete_session(del_id))
    for s in (await h2.list_sessions())["sessions"]:
        print(f"  active={s['active']} id={s['id'][:8]}")
    await h2.aclose()

    shutil.rmtree(root, ignore_errors=True)


asyncio.run(main())
