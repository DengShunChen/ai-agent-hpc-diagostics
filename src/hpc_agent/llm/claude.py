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

import os
from typing import Any

from hpc_agent.ingest.deidentify import find_identifiers
from hpc_agent.llm.provider import LLMConfigError, LLMUnavailableError
from hpc_agent.safety.secrets import find_credential_like

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 16000
CLOUD_DATA_CLASSES = ("synthetic", "deidentified-import")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class OutboundBlockedError(LLMUnavailableError):
    """A request was not sent because it may carry sensitive or identifying data."""


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

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, effort: str = DEFAULT_EFFORT, client: Any = None) -> None:
        if not model.startswith("claude-"):
            raise LLMConfigError(f"not a Claude model id: {model!r}")
        if effort not in EFFORTS:
            raise LLMConfigError(f"effort must be one of {EFFORTS}")
        if client is None:
            import anthropic  # imported lazily: the platform runs without it when Claude isn't selected

            client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self._client = client

    def _send(self, system: str, messages: list[dict], tools: list[dict]) -> Any:
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
    return ClaudeProvider(
        model=os.environ.get("MODEL_NAME") or DEFAULT_CLAUDE_MODEL,
        effort=(os.environ.get("HPC_AGENT_CLAUDE_EFFORT") or DEFAULT_EFFORT).strip().lower(),
    )


__all__ = ["ClaudeProvider", "OutboundBlockedError", "claude_from_env", "cloud_data_problem"]
