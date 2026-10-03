"""Settings for the harness: model, limits, thinking, sampling."""
from __future__ import annotations

from dataclasses import dataclass, asdict

# mtplx server facts (verified via GET /health on 2026-09-30):
SERVER_CONTEXT_WINDOW = 204_800   # model supports 262144, machine-bound at 204.8K
SAFETY_MARGIN = 2_048            # keep some room below the server window
VALID_EFFORTS = ("", "minimal", "low", "medium", "xhigh")


@dataclass
class Settings:
    # Model / endpoint (pi's models.json mapping, see docs/MTPLX.md)
    model: str = "mtplx-qwen38-27b-optimized-speed"
    base_url: str = "http://127.0.0.1:8000"
    api_key: str = "mtplx-local"
    client_header: str = "llm-harness"

    # Context / output limits
    max_context_tokens: int = 60_000   # compaction boundary (B)
    max_output_tokens: int = 8_192     # max_tokens in the request (O)

    # Thinking (qwen flat fields, see docs/MTPLX.md)
    thinking_enabled: bool = True
    thinking_effort: str = "medium"     # "" | minimal | low | medium | xhigh

    # Sampling (None = server default: temp 1.0, top_p 0.95, top_k 20)
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None

    def validate(self) -> None:
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be > 0")
        if self.max_context_tokens > SERVER_CONTEXT_WINDOW:
            raise ValueError(
                f"max_context_tokens > server window ({SERVER_CONTEXT_WINDOW})")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be > 0")
        if self.max_context_tokens + self.max_output_tokens > SERVER_CONTEXT_WINDOW - SAFETY_MARGIN:
            raise ValueError(
                f"max_context_tokens + max_output_tokens must be <= "
                f"{SERVER_CONTEXT_WINDOW - SAFETY_MARGIN}")
        if self.thinking_effort not in VALID_EFFORTS:
            raise ValueError(f"thinking_effort must be one of {VALID_EFFORTS}")

    def to_dict(self) -> dict:
        return asdict(self)