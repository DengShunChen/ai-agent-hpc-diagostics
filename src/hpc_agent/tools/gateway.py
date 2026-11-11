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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.models.audit import ToolInvocationRecord
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
# Every argument type a tool may declare. Free text is limited to search terms: no shell
# metacharacters, quotes, newlines or path traversal can pass.
_ARG_PATTERNS = {
    "incident_id": re.compile(INCIDENT_ID_PATTERN),
    "query": re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,199}"),
    "skill": re.compile(r"[a-z][a-z0-9-]{1,63}"),
    # Log search: a fixed list of sources, and keywords with the same character set as a query.
    "source": re.compile(r"slurmctld|slurmd|pjm|syslog"),
    "keywords": re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,119}"),
}
DEFAULT_CALL_BUDGET = 40


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

    def describe(self, name: str) -> tuple[str, frozenset[str]]:
        """A registered tool's description and argument names (for offering it to a model)."""
        spec = self._registry.get(name)
        if spec is None:
            raise ToolNotAllowedError(f"tool not registered: {name!r}")
        return spec.description, spec.parameters

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
            if not isinstance(value, str) or not _ARG_PATTERNS[key].fullmatch(value) or ".." in value:
                raise ToolArgumentError(f"invalid value for argument {key!r}")
        return spec.handler(**kwargs)


class CallBudget:
    """A per-run limit on tool calls, shared by all agents in the run."""

    def __init__(self, limit: int = DEFAULT_CALL_BUDGET) -> None:
        self.remaining = limit

    def spend(self, agent: str, tool: str) -> None:
        if self.remaining <= 0:
            raise ToolNotAllowedError(f"tool call budget exhausted ({agent} -> {tool})")
        self.remaining -= 1


class ScopedGateway:
    """An agent's view of the gateway: only its configured tools, every call recorded.

    The scope is intersected with the gateway's registry, so configuration can narrow
    access but never widen it. An agent with no configured scope gets no tools.
    """

    def __init__(
        self,
        gateway: ToolGateway,
        agent: str,
        scopes: Mapping[str, frozenset[str]],
        log: list[ToolInvocationRecord],
        budget: CallBudget | None = None,
    ) -> None:
        self._gateway = gateway
        self.agent = agent
        self.allowed_tools = scopes.get(agent, frozenset()) & set(gateway.registered_tools())
        self._log = log
        self._budget = budget or CallBudget()

    def describe(self, tool: str) -> tuple[str, frozenset[str]]:
        if tool not in self.allowed_tools:
            raise ToolNotAllowedError(f"{self.agent} is not permitted to call {tool!r}")
        return self._gateway.describe(tool)

    def invoke(self, tool: str, **kwargs: str) -> ToolResult:
        if tool not in self.allowed_tools:
            raise ToolNotAllowedError(f"{self.agent} is not permitted to call {tool!r}")
        self._budget.spend(self.agent, tool)
        start = time.perf_counter()
        try:
            result = self._gateway.invoke(tool, **kwargs)
        except Exception:
            self._record(tool, "error", None, start)
            raise
        self._record(tool, result.status, result.source, start)
        return result

    def _record(self, tool: str, status: str, reference: str | None, start: float) -> None:
        self._log.append(
            ToolInvocationRecord(
                tool=tool,
                agent=self.agent,
                result_status=status,
                evidence_reference=reference,
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        )


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
    from hpc_agent.tools import (
        gpu_tools, knowledge_tools, log_tools, network_tools, scheduler_tools, skill_tools, storage_tools,
    )

    adapter = adapter or ReadOnlyDataAdapter()
    gateway = ToolGateway()
    for module in (storage_tools, scheduler_tools, network_tools, gpu_tools, log_tools, knowledge_tools, skill_tools):
        for spec in module.tool_specs(adapter):
            gateway.register(spec)
    return gateway
