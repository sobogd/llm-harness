"""Multi-session lifecycle test: archive, list, load, delete (no LLM calls)."""
import asyncio
import sys
import tempfile

sys.path.insert(0, ".")
from llm_harness.harness import Harness  # noqa: E402


def seed(h: Harness, first_user: str) -> None:
    m = {"role": "user", "content": first_user}
    h.history.append(m)
    h.store.record_message(m)
    m = {"role": "assistant", "content": "reply to " + first_user}
    h.history.append(m)
    h.store.record_message(m)
    h.store.record_run(h.run_id or "r-test", "done", 1, "")


async def main() -> None:
    root = tempfile.mkdtemp(prefix="sess-test-")
    h = Harness(root)
    await h.startup()          # records meta + system prompt (main() does this)
    await h.store.flush()
    id_a = h.session_id
    seed(h, "first question of A")

    r = await h.new_session()
    id_b = r["session_id"]
    assert id_b != id_a
    seed(h, "first question of B")

    r = await h.new_session()
    id_c = r["session_id"]
    assert id_c not in (id_a, id_b)
    # C is fresh (system prompt only) -> NOT archived
    await h.store.flush()

    # --- list
    lst = await h.list_sessions()
    by_id = {s["id"]: s for s in lst["sessions"]}
    assert set(by_id) == {id_a, id_b, id_c}, by_id
    assert by_id[id_c]["active"] is True
    assert by_id[id_a]["active"] is False
    assert by_id[id_a]["preview"].startswith("first question of A")
    assert by_id[id_a]["messages"] == 3      # system + user + assistant
    order = [s["id"] for s in lst["sessions"]]
    assert order == [id_c, id_b, id_a], order   # newest activity first
    print("list: OK")

    # --- load (current fresh session is dropped, it has no content)
    r = await h.load_session(id_a)
    assert r["loaded"] is True and r["session_id"] == id_a
    assert h.session_id == id_a
    assert h.history[1]["content"] == "first question of A"
    assert h.state == "idle"

    # loading the current session is a no-op
    r = await h.load_session(id_a)
    assert r["loaded"] is False
    print("load: OK")

    # --- refuse while a run is active
    h.state = "running"
    try:
        await h.load_session(id_b)
        raise AssertionError("load_session must refuse while running")
    except ValueError:
        pass
    try:
        await h.delete_session(id_a)
        raise AssertionError("delete of current session must refuse while running")
    except ValueError:
        pass
    h.state = "idle"
    print("guard: OK")

    # --- delete archived
    r = await h.delete_session(id_b)
    assert r["ok"] and r["deleted"] == id_b and r["session_id"] == id_a
    by_id = {s["id"]: s for s in (await h.list_sessions())["sessions"]}
    assert set(by_id) == {id_a}
    try:
        await h.delete_session(id_b)   # twice -> not found
        raise AssertionError("second delete must fail")
    except ValueError:
        pass
    print("delete archived: OK")

    # --- delete current -> fresh session
    r = await h.delete_session(id_a)
    assert r["ok"] and r["deleted"] == id_a
    id_d = r["session_id"]
    assert id_d not in (id_a, id_b, id_c)
    assert h.history == [{"role": "system", "content": h.history[0]["content"]}]
    by_id = {s["id"]: s for s in (await h.list_sessions())["sessions"]}
    assert set(by_id) == {id_d} and by_id[id_d]["active"] is True
    print("delete current: OK")

    # --- new_session with nothing to archive is fine
    r = await h.new_session()
    assert r["session_id"] != id_d
    by_id = {s["id"]: s for s in (await h.list_sessions())["sessions"]}
    assert set(by_id) == {r["session_id"]}

    await h.aclose()
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())