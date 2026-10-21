"""LLM provider abstraction and a local-only Ollama client.

The provider sends a system prompt and a user prompt and returns text. It never sends tool
definitions, so the model cannot call anything. Only local endpoints are accepted, and
Ollama ":cloud" models (which forward requests to a remote service) are refused unless
HPC_AGENT_LLM_ALLOW_CLOUD=1 is set explicitly.

Configuration (environment, see .env.example):
    MODEL_PROVIDER=ollama   MODEL_NAME=qwen2.5:3b   MODEL_ENDPOINT=http://127.0.0.1:11434
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urlsplit

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.containers.internal"})
DEFAULT_MODEL = "qwen2.5:3b"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_S = 300.0
MAX_RESPONSE_BYTES = 256 * 1024


class LLMConfigError(ValueError):
    pass


class LLMUnavailableError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str
    model: str

    def generate(self, system: str, prompt: str) -> str: ...


def is_cloud_model(model: str) -> bool:
    tag = model.rsplit(":", 1)[-1].lower() if ":" in model else ""
    return tag == "cloud" or tag.endswith("-cloud")


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        allow_cloud: bool = False,
        max_tokens: int = 700,
    ) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme != "http" or parts.hostname not in LOCAL_HOSTS or parts.path not in ("", "/"):
            raise LLMConfigError(f"Ollama endpoint must be a local http URL, got {endpoint!r}")
        if is_cloud_model(model) and not allow_cloud:
            raise LLMConfigError(
                f"{model!r} is an Ollama cloud model: requests would leave this machine. "
                "Use a local model, or set HPC_AGENT_LLM_ALLOW_CLOUD=1 to allow it explicitly."
            )
        self.model = model
        self._url = f"{parts.scheme}://{parts.netloc}/api/chat"
        self._timeout = timeout_s
        self._max_tokens = max_tokens

    def generate(self, system: str, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            # Deterministic decoding; no "tools" field is ever sent.
            "options": {"temperature": 0, "seed": 42, "num_predict": self._max_tokens},
        }).encode()
        request = urllib.request.Request(self._url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 (local only)
                payload = json.loads(response.read(MAX_RESPONSE_BYTES))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            raise LLMUnavailableError(f"Ollama request failed: {error}") from None
        content = (payload.get("message") or {}).get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise LLMUnavailableError("Ollama returned no message content")
        return content


def provider_from_env() -> LLMProvider | None:
    """The configured provider, or None when no LLM is configured (the LLM is optional)."""
    name = os.environ.get("MODEL_PROVIDER", "").strip().lower()
    if not name:
        return None
    if name != "ollama":
        raise LLMConfigError(f"unsupported MODEL_PROVIDER {name!r} (supported: ollama)")
    return OllamaProvider(
        model=os.environ.get("MODEL_NAME") or DEFAULT_MODEL,
        endpoint=os.environ.get("MODEL_ENDPOINT") or DEFAULT_ENDPOINT,
        timeout_s=float(os.environ.get("HPC_AGENT_LLM_TIMEOUT", DEFAULT_TIMEOUT_S)),
        allow_cloud=os.environ.get("HPC_AGENT_LLM_ALLOW_CLOUD") == "1",
    )
