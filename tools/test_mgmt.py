"""Management smoke test: SetSettings, Stop/Resume, Compact, Status, queued Ask."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grpc
from llm_harness import llm_harness_pb2 as pb
from llm_harness import llm_harness_pb2_grpc as pb_grpc

with grpc.insecure_channel("127.0.0.1:9000") as ch:
    stub = pb_grpc.HarnessStub(ch)

    # 1. SetSettings: thinking off, smaller output
    s = stub.SetSettings(pb.Settings(thinking_enabled=False,
                                     max_output_tokens=2000,
                                     thinking_effort=""))
    print(f"1. SET settings: thinking={s.thinking_enabled} "
          f"effort={s.thinking_effort!r} max_out={s.max_output_tokens}")

    # invalid settings must abort
    try:
        stub.SetSettings(pb.Settings(max_context_tokens=999999999))
        print("2. invalid settings: NOT REJECTED (bad)")
    except grpc.RpcError as e:
        print(f"2. invalid settings rejected: {e.details()[:80]}")

    # 3. Start a long task, then stop mid-run
    r = stub.Ask(pb.AskRequest(prompt=(
        "Write a Python file sieve.py implementing the Sieve of Eratosthenes, "
        "then write a separate detailed 1000-word essay about the history of "
        "prime numbers to essay.txt, section by section (write it in several "
        "bash append commands so it takes time).")))
    print(f"3. ASK long task -> run_id={r.run_id[:8]} state={r.state}")
    time.sleep(8)
    st = stub.Stop(pb.Empty())
    print(f"4. STOP -> {pb.RunState.Name(st.state)} turn={st.turn} err={st.last_error!r}")

    # 5. Queued ask while stopped should start a new run after drain
    #    (queue was empty; now ask again while stopped -> fresh run)
    # 6. Resume the stopped run
    st = stub.Resume(pb.Empty())
    print(f"6. RESUME -> {pb.RunState.Name(st.state)}")
    deadline = time.time() + 240
    while time.time() < deadline:
        st = stub.Status(pb.Empty())
        if pb.RunState.Name(st.run.state) in (
                "RUN_STATE_DONE", "RUN_STATE_STOPPED", "RUN_STATE_ERROR"):
            break
        time.sleep(2)
    print(f"7. after resume: {pb.RunState.Name(st.run.state)} turn={st.run.turn} "
          f"hist={st.history_messages} err={st.run.last_error!r}")

    # 8. Manual compact
    c = stub.Compact(pb.CompactRequest(keep_last_messages=6))
    print(f"8. COMPACT ok={c.ok} tokens {c.tokens_before}->{c.tokens_after} "
          f"msgs {c.messages_before}->{c.messages_after} "
          f"preview={c.summary_preview[:60]!r}")

    st = stub.Status(pb.Empty())
    print(f"9. STATUS hist={st.history_messages} prompt_tokens_last="
          f"{st.prompt_tokens_last} queue={st.queue_depth}")