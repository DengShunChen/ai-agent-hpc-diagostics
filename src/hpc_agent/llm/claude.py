"""Claude provider (MODEL_PROVIDER=anthropic): the optional cloud model of the hybrid setup.

Local Ollama stays the default. This provider sends requests to Anthropic's API through the
official `anthropic` SDK, so it is used only when an operator selects it, and only on data
whose fixtures/DATA_CLASS.json says "synthetic" or "deidentified-import" (checked by the
caller with `cloud_data_problem`). Every request is also scanned just before it leaves:
anything credential-like or identifying (IP or e-mail addresses, domain names, home paths)
blocks the request, and the agent falls back to its rules.

Like the Ollama provider, it only returns the model's tool calls; the harness decides what
runs. Assistant turns are passed back unchanged (`provider_state`), as the API requires for
thinking blocks. Credentials come from the SDK's usual sources (ANTHROPIC_API_KEY or an
`ant auth login` profile) and are never written anywhere by this platform.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from hpc_agent.ingest.deidentify import find_identifiers
from hpc_agent.llm.provider import LLMConfigError, LLMUnavailableError
from hpc_agent.safety.secrets import find_credential_like

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 16000
CLOUD_DATA_CLASSES = ("synthetic", "deidentified-import")
# USD per million tokens (input, output), standard first-party rates; used for the spend cap.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class OutboundBlockedError(LLMUnavailableError):
    """A request was not sent because it may carry sensitive or identifying data."""


class BudgetExceededError(LLMUnavailableError):
    """The spend cap was reached; no further requests are sent (agents fall back to rules)."""


def cloud_data_problem(data_class: str | None) -> str | None:
    """Why this data root must not be sent to a cloud model, or None if it may."""
    if data_class in CLOUD_DATA_CLASSES:
        return None
    return ("Claude needs fixtures/DATA_CLASS.json to say 'synthetic' or 'deidentified-import' "
            "(import the data with `hpc-agent import-export` first)")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    if hasattr(value, "model_dump"):
        return _strings(value.model_dump())
    return []


def outbound_problems(payload: Any) -> list[str]:
    """Kinds of sensitive content in what is about to be sent (never the values)."""
    texts = _strings(payload)
    problems = []
    if any(find_credential_like(t) for t in texts):
        problems.append("credential-like content")
    kinds = sorted({hit.rsplit("(", 1)[-1].rstrip(")") for t in texts for hit in find_identifiers(t)})
    problems += kinds
    return problems


def to_claude_tools(tools: list[dict] | None) -> list[dict]:
    """Ollama/OpenAI-style function specs -> Claude tool definitions (strict schemas)."""
    out = []
    for spec in tools or []:
        function = spec.get("function", {})
        schema = dict(function.get("parameters") or {"type": "object", "properties": {}})
        schema["additionalProperties"] = False
        schema.setdefault("required", [])
        out.append({"name": function["name"], "description": function.get("description", ""),
                    "input_schema": schema, "strict": True})
    return out


def to_claude_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """The harness's conversation -> (system, Claude messages).

    Assistant turns are sent back as the exact content blocks Claude returned. Consecutive tool
    results go into one user message (tool results first, then any text), as the API expects.
    """
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: list[dict] = []
    pending: list[dict] = []

    def flush(extra_text: str | None = None) -> None:
        nonlocal pending
        content = list(pending)
        if extra_text is not None:
            content.append({"type": "text", "text": extra_text})
        if content:
            out.append({"role": "user", "content": content})
        pending = []

    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        if role == "tool":
            pending.append({"type": "tool_result", "tool_use_id": message["tool_call_id"],
                            "content": message["content"]})
        elif role == "user":
            flush(message["content"])
        elif role == "assistant":
            flush()
            out.append({"role": "assistant", "content": message.get("provider_state") or message["content"]})
    flush()
    return system, out


class ClaudeProvider:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, effort: str = DEFAULT_EFFORT, client: Any = None,
                 budget_usd: float | None = None, ledger: Path | None = None) -> None:
        if not model.startswith("claude-"):
            raise LLMConfigError(f"not a Claude model id: {model!r}")
        if budget_usd is not None and model not in PRICES:
            raise LLMConfigError(f"no price known for {model!r}; a spend cap needs one of {sorted(PRICES)}")
        self.budget_usd = budget_usd
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        # Spend across runs (the cap applies to the total, not just this process).
        self.ledger = ledger
        if effort not in EFFORTS:
            raise LLMConfigError(f"effort must be one of {EFFORTS}")
        if client is None:
            import anthropic  # imported lazily: the platform runs without it when Claude isn't selected

            client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self._client = client

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        tokens_in = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        price_in, price_out = PRICES.get(self.model, (0.0, 0.0))
        self.usage["requests"] += 1
        self.usage["input_tokens"] += tokens_in
        self.usage["output_tokens"] += tokens_out
        cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
        self.usage["cost_usd"] += cost
        if self.ledger is not None:
            self._ledger_add(cost)

    def _ledger_total(self) -> float:
        if self.ledger is None or not self.ledger.exists():
            return 0.0
        try:
            return float(json.loads(self.ledger.read_text(encoding="utf-8")).get("cost_usd", 0.0))
        except (ValueError, OSError, AttributeError):
            raise BudgetExceededError("spend ledger unreadable; refusing to send (fix or remove it by hand)") from None

    def _ledger_add(self, cost: float) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.ledger, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            raw = handle.read()
            data = json.loads(raw) if raw.strip() else {"cost_usd": 0.0, "requests": 0}
            data["cost_usd"] = float(data.get("cost_usd", 0.0)) + cost
            data["requests"] = int(data.get("requests", 0)) + 1
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(data))

    def spent_usd(self) -> float:
        """Total spend the cap counts: the ledger's total when there is one, else this process."""
        return self._ledger_total() if self.ledger is not None else self.usage["cost_usd"]

    def _send(self, system: str, messages: list[dict], tools: list[dict]) -> Any:
        if self.budget_usd is not None and self.spent_usd() >= self.budget_usd:
            raise BudgetExceededError(f"Claude spend cap reached (${self.spent_usd():.2f} of ${self.budget_usd:.2f})")
        # Assistant turns are Claude's own earlier output; everything else is checked before it leaves.
        problems = outbound_problems([system, tools, [m for m in messages if m["role"] == "user"]])
        if problems:
            raise OutboundBlockedError(f"request not sent to Claude: {', '.join(problems)}")
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": MAX_TOKENS, "system": system, "messages": messages,
            "output_config": {"effort": self.effort},
            # On a policy decline the API retries on a fallback model it picks by category.
            "betas": [_FALLBACK_BETA], "fallbacks": "default",
        }
        if tools:
            kwargs["tools"] = tools
        try:
            response = self._client.beta.messages.create(**kwargs)
            self._record_usage(response)
        except (anthropic.APIConnectionError, anthropic.APIStatusError, anthropic.CredentialsError) as error:
            raise LLMUnavailableError(f"Claude request failed: {type(error).__name__}") from None
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise LLMUnavailableError(f"Claude declined the request ({category or 'no category'})")
        if response.stop_reason == "max_tokens":
            raise LLMUnavailableError("Claude reply hit max_tokens")
        return response

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_output: bool = False) -> dict:
        system, claude_messages = to_claude_messages(messages)
        response = self._send(system, claude_messages, to_claude_tools(tools))
        text = "".join(block.text for block in response.content if block.type == "text")
        calls = [{"id": block.id, "name": block.name, "arguments": block.input if isinstance(block.input, dict) else {}}
                 for block in response.content if block.type == "tool_use"]
        return {"content": text, "tool_calls": [] if json_output and not calls else calls,
                "provider_state": list(response.content)}

    def generate(self, system: str, prompt: str) -> str:
        response = self._send(system, [{"role": "user", "content": prompt}], [])
        return "".join(block.text for block in response.content if block.type == "text")


def claude_from_env() -> ClaudeProvider:
    budget = os.environ.get("HPC_AGENT_CLAUDE_BUDGET_USD")
    try:
        budget_usd = float(budget) if budget else None
    except ValueError:
        raise LLMConfigError("HPC_AGENT_CLAUDE_BUDGET_USD must be a number") from None
    ledger = os.environ.get("HPC_AGENT_CLAUDE_LEDGER") or str(Path.home() / ".config" / "hpc-agent" / "claude-spend.json")
    return ClaudeProvider(
        model=os.environ.get("MODEL_NAME") or DEFAULT_CLAUDE_MODEL,
        effort=(os.environ.get("HPC_AGENT_CLAUDE_EFFORT") or DEFAULT_EFFORT).strip().lower(),
        budget_usd=budget_usd,
        ledger=Path(ledger) if budget_usd is not None else None,
    )


__all__ = ["ClaudeProvider", "OutboundBlockedError", "claude_from_env", "cloud_data_problem"]
