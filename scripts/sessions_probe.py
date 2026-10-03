#!/Users/sobogd/work/llm-harness/.venv/bin/python
"""Session-management RPC probe for llm-harness.

Usage: python sessions_probe.py [host:port]
  1) Status
  2) ListSessions
  3) if idle: NewSession -> verify in list -> DeleteSession -> verify gone
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grpc

from llm_harness import llm_harness_pb2 as pb
from llm_harness import llm_harness_pb2_grpc as pb_grpc


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:9000"
    ch = grpc.insecure_channel(target)
    st = pb_grpc.HarnessStub(ch)
    ok = True

    def call(name, fn):
        nonlocal ok
        try:
            r = fn()
            print(f"{name}: OK")
            return r
        except grpc.RpcError as e:
            ok = False
            print(f"{name}: FAIL {e.code().name} {e.details()[:120]}")
            return None

    s = call("Status", lambda: st.Status(pb.Empty()))
    if s:
        print(f"  session={s.session_id!r} run={s.run.state.name} "
              f"hist={s.history_messages} loaded_from={s.loaded_from!r}")
    ls = call("ListSessions", lambda: st.ListSessions(pb.Empty()))
    if ls:
        print(f"  n={len(ls.sessions)}")
        for x in ls.sessions:
            print(f"  - {x.id} active={x.active} msgs={x.messages} "
                  f"preview={x.preview[:50]!r}")

    idle = bool(s) and s.run.state == pb.RUN_STATE_IDLE
    if idle:
        ns = call("NewSession", lambda: st.NewSession(pb.Empty()))
        if ns:
            sid = ns.session_id
            print(f"  new session {sid} state={ns.state}")
            time.sleep(0.3)
            ls2 = call("ListSessions-after", lambda: st.ListSessions(pb.Empty()))
            if ls2:
                present = any(x.id == sid for x in ls2.sessions)
                print(f"  new session present in list: {present}")
                ok = ok and present
            d = call("DeleteSession",
                     lambda: st.DeleteSession(pb.DeleteSessionRequest(session_id=sid)))
            if d:
                print(f"  delete ok={d.ok}")
                time.sleep(0.3)
                ls3 = call("ListSessions-final", lambda: st.ListSessions(pb.Empty()))
                if ls3:
                    still = any(x.id == sid for x in ls3.sessions)
                    print(f"  new session gone: {not still}")
                    ok = ok and not still
    else:
        print("(skipping NewSession/DeleteSession: run not idle)")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())