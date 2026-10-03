"""Manual gRPC client for smoke tests.

Usage: python tools/ask_client.py [HOST:PORT] "prompt" ["prompt 2"...]
Defaults: 127.0.0.1:9000; waits for the run to finish and prints final status.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grpc
from llm_harness import llm_harness_pb2 as pb
from llm_harness import llm_harness_pb2_grpc as pb_grpc

HOST = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "127.0.0.1:9000"
argi = 1 if HOST == sys.argv[1] else 0
prompts = sys.argv[1 + argi:] or ["Say the word OK and nothing else."]

with grpc.insecure_channel(HOST) as ch:
    stub = pb_grpc.HarnessStub(ch)
    for p in prompts:
        r = stub.Ask(pb.AskRequest(prompt=p))
        print(f"ASK -> run_id={r.run_id} state={r.state}")
    while True:
        st = stub.Status(pb.Empty())
        state = pb.RunState.Name(st.run.state)
        if state in ("RUN_STATE_DONE", "RUN_STATE_STOPPED", "RUN_STATE_ERROR"):
            break
        time.sleep(2)
    print(f"FINAL {state} run={st.run.run_id} turn={st.run.turn} "
          f"prompt_tokens_last={st.prompt_tokens_last} "
          f"hist={st.history_messages} err={st.run.last_error!r}")
