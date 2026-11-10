"""Per-run trace of what each LLM-driven agent did: the prompt, every model reply, every tool
call with the hook decision, what the checks kept or discarded, and any fallback to rules.

Events are collected in memory during the run; the approval service stores them in a
hash-chained log (audit-data/traces/<run_id>.jsonl). Credential-like content is withheld and
long text is shortened before an event is kept.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from hpc_agent.safety.secrets import SENSITIVE_NOTICE, find_credential_like

MAX_TEXT = 4000
Emit = Callable[..., None]


def _shorten(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + f"... [{len(value) - MAX_TEXT} more chars]"
    if isinstance(value, dict):
        return {str(k): _shorten(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shorten(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class TraceRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, agent: str, kind: str, **detail: Any) -> None:
        detail = _shorten(detail)
        if find_credential_like(detail):
            detail = {"withheld": SENSITIVE_NOTICE}
        self.events.append({"seq": len(self.events), "at": datetime.now(UTC).isoformat(),
                            "agent": agent, "kind": kind, **detail})

    def for_agent(self, agent: str) -> Emit:
        return lambda kind, **detail: self.record(agent, kind, **detail)


def no_trace(kind: str, **detail: Any) -> None:
    """Default when nobody records."""
