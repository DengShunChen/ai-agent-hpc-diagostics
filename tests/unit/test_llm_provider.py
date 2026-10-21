import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hpc_agent.llm.provider import (
    LLMConfigError,
    LLMUnavailableError,
    OllamaProvider,
    is_cloud_model,
    provider_from_env,
)


@pytest.mark.parametrize("endpoint", ["http://10.0.0.5:11434", "https://127.0.0.1:11434", "http://ollama.example:11434",
                                      "http://127.0.0.1:11434/v1", "ftp://127.0.0.1"])
def test_only_local_http_endpoints(endpoint):
    with pytest.raises(LLMConfigError):
        OllamaProvider(endpoint=endpoint)


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:11434", "http://localhost:11434/", "http://[::1]:11434",
                                      "http://host.containers.internal:11434"])
def test_local_endpoints_accepted(endpoint):
    OllamaProvider(endpoint=endpoint)


@pytest.mark.parametrize("model,cloud", [("kimi-k2.5:cloud", True), ("gpt-oss:120b-cloud", True),
                                         ("qwen2.5:3b", False), ("llama3.2", False)])
def test_cloud_models_refused_by_default(model, cloud):
    assert is_cloud_model(model) is cloud
    if cloud:
        with pytest.raises(LLMConfigError, match="leave this machine"):
            OllamaProvider(model=model)
        OllamaProvider(model=model, allow_cloud=True)


def test_provider_from_env(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert provider_from_env() is None
    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_NAME", "qwen2.5:3b")
    assert provider_from_env().model == "qwen2.5:3b"
    monkeypatch.setenv("MODEL_NAME", "kimi-k2.5:cloud")
    with pytest.raises(LLMConfigError):
        provider_from_env()
    monkeypatch.setenv("MODEL_PROVIDER", "azure-openai")
    with pytest.raises(LLMConfigError, match="unsupported"):
        provider_from_env()


@pytest.fixture
def fake_ollama():
    """A local HTTP server standing in for Ollama; records the request it receives."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = json.dumps({"message": {"role": "assistant", "content": "說明 [E1]"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def test_request_shape_has_no_tools(fake_ollama):
    endpoint, seen = fake_ollama
    assert OllamaProvider(endpoint=endpoint).generate("system", "prompt") == "說明 [E1]"
    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["stream"] is False and "tools" not in body
    assert body["options"]["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_unreachable_endpoint_is_unavailable():
    with pytest.raises(LLMUnavailableError):
        OllamaProvider(endpoint="http://127.0.0.1:9", timeout_s=2).generate("s", "p")
