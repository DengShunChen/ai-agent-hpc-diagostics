"""Configuration loading.

config/agents.yaml can only *narrow* which registered gateway tools an agent may call;
it cannot add tools to the gateway allowlist. Malformed config fails closed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hpc_agent.data_adapter import default_data_root


class ConfigError(ValueError):
    pass


def load_agent_tool_scopes(root: Path | None = None) -> dict[str, frozenset[str]]:
    path = (root or default_data_root()) / "config" / "agents.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        raise ConfigError("agents.yaml must contain an 'agents' mapping")
    scopes: dict[str, frozenset[str]] = {}
    for name, spec in agents.items():
        tools = spec.get("tools", []) if isinstance(spec, dict) else None
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ConfigError(f"agents.yaml: 'tools' for {name!r} must be a list of names")
        scopes[name] = frozenset(tools)
    return scopes
