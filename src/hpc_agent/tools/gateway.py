"""Allowlisted Tool Gateway.

Agents reach operational data only through ToolGateway.invoke(). The gateway:
  * looks tools up in an explicit registry, so unknown names fail closed;
  * refuses to register shell/exec/ssh-style tools under any name;
  * checks every call against the SafetyPolicy;
  * accepts only declared, string-typed, pattern-restricted arguments.
There is no shell backend: handlers are plain Python functions reading saved data.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.models.incident import INCIDENT_ID_PATTERN
from hpc_agent.safety.policy import Operation, SafetyPolicy

FORBIDDEN_TOOL_NAMES = frozenset(
    {"run_shell", "execute_command", "ssh_execute", "run_script", "run_linux_command"}
)
# Substrings that never belong in a read-only tool name.
_FORBIDDEN_NAME_FRAGMENTS = (
    "shell", "exec", "ssh", "script", "command", "cmd", "sudo", "eval", "subprocess", "popen",
)
# Whole name tokens (split on "_"); matched as tokens so "get_filesystem_usage" stays valid.
_FORBIDDEN_NAME_TOKENS = frozenset(
    {"os", "system", "run", "spawn", "kill", "reboot", "restart", "delete", "rm", "write", "set"}
)
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ARG_PATTERNS = {"incident_id": re.compile(INCIDENT_ID_PATTERN)}


class ToolNotAllowedError(PermissionError):
    pass


class ToolArgumentError(ValueError):
    pass


class ToolStatus(StrEnum):
    OK = "ok"
    NO_DATA = "no_data"  # nothing saved for this request; never filled with invented values


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    status: ToolStatus
    source: str | None = None  # evidence reference, e.g. "test_data/gpu/POC-GPU-001/gpu_health.json"
    data: Any = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    operation: Operation
    parameters: frozenset[str]
    handler: Callable[..., ToolResult]


def is_forbidden_tool_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in FORBIDDEN_TOOL_NAMES
        or any(f in lowered for f in _FORBIDDEN_NAME_FRAGMENTS)
        or not _FORBIDDEN_NAME_TOKENS.isdisjoint(lowered.split("_"))
    )


class ToolGateway:
    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self._policy = policy or SafetyPolicy()
        self._registry: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not _TOOL_NAME.match(spec.name):
            raise ToolNotAllowedError(f"invalid tool name: {spec.name!r}")
        if is_forbidden_tool_name(spec.name):
            raise ToolNotAllowedError(f"tool name is forbidden: {spec.name!r}")
        if spec.name in self._registry:
            raise ToolNotAllowedError(f"tool already registered: {spec.name!r}")
        if not self._policy.is_allowed(spec.operation):
            raise ToolNotAllowedError(f"tool operation denied by policy: {spec.operation}")
        unknown = spec.parameters - _ARG_PATTERNS.keys()
        if unknown:
            raise ToolNotAllowedError(f"undeclared argument types: {sorted(unknown)}")
        self._registry[spec.name] = spec

    def registered_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._registry))

    def invoke(self, name: object, **kwargs: object) -> ToolResult:
        spec = self._registry.get(name) if isinstance(name, str) else None
        if spec is None:
            raise ToolNotAllowedError(f"tool not registered: {name!r}")
        self._policy.require(spec.operation)
        if set(kwargs) != spec.parameters:
            raise ToolArgumentError(
                f"{name} expects arguments {sorted(spec.parameters)}, got {sorted(kwargs)}"
            )
        for key, value in kwargs.items():
            if not isinstance(value, str) or not _ARG_PATTERNS[key].match(value):
                raise ToolArgumentError(f"invalid value for argument {key!r}")
        return spec.handler(**kwargs)


def read_saved_result(
    adapter: ReadOnlyDataAdapter, tool: str, domain: str, incident_id: str, dataset: str
) -> ToolResult:
    segments = ("test_data", domain, incident_id, f"{dataset}.json")
    if not adapter.exists(*segments):
        return ToolResult(tool=tool, status=ToolStatus.NO_DATA)
    return ToolResult(
        tool=tool, status=ToolStatus.OK, source="/".join(segments), data=adapter.read_json(*segments)
    )


def saved_data_tool(
    adapter: ReadOnlyDataAdapter, name: str, description: str, domain: str, dataset: str
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        operation=Operation.READ_TEST_DATA,
        parameters=frozenset({"incident_id"}),
        handler=lambda incident_id: read_saved_result(adapter, name, domain, incident_id, dataset),
    )


def build_default_gateway(adapter: ReadOnlyDataAdapter | None = None) -> ToolGateway:
    from hpc_agent.tools import gpu_tools, network_tools, scheduler_tools, storage_tools

    adapter = adapter or ReadOnlyDataAdapter()
    gateway = ToolGateway()
    for module in (storage_tools, scheduler_tools, network_tools, gpu_tools):
        for spec in module.tool_specs(adapter):
            gateway.register(spec)
    return gateway
