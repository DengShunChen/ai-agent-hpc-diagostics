"""Tools as the model sees them, and how the harness runs the calls it asks for.

The model only names a tool. Arguments come from the harness: data tools always get this
incident's ID; a knowledge query is normalised and must still pass the gateway's argument
pattern. Every call goes through the agent's ScopedGateway, so its scope, the run's call
budget and the tool-call log apply unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from hpc_agent.models import Incident
from hpc_agent.runtime.hooks import CallContext, post_tool_use, pre_tool_use
from hpc_agent.runtime.trace import Emit, no_trace
from hpc_agent.tools.gateway import ScopedGateway, ToolArgumentError, ToolNotAllowedError, ToolResult, ToolStatus

MAX_RESULT_CHARS = 3000


@dataclass
class ToolSession:
    """What one agent fetched during one run: data results by reference (T1, T2, ...)."""

    max_calls: int
    ask: frozenset[str] = frozenset()  # tools that need a human (never run unattended)
    emit: Emit = no_trace
    fetched: dict[str, ToolResult] = field(default_factory=dict)
    no_data: list[str] = field(default_factory=list)
    knowledge: list[ToolResult] = field(default_factory=list)
    calls: int = 0


def tool_specs(gateway: ScopedGateway) -> list[dict]:
    """JSON-schema tool definitions for the agent's own scope (data tools take no arguments)."""
    specs = []
    for tool in sorted(gateway.allowed_tools):
        description, params = gateway.describe(tool)
        properties = {"query": {"type": "string", "description": "a few keywords"}} if "query" in params else {}
        specs.append({"type": "function", "function": {
            "name": tool, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": sorted(properties)},
        }})
    return specs


def run_tool(name: str, arguments: dict, incident: Incident, gateway: ScopedGateway, session: ToolSession,
             default_query: str) -> str:
    """Run one requested call (PreToolUse hooks, the call, PostToolUse hooks); return what the model sees."""
    ctx = CallContext(agent=gateway.agent, tool=name, gateway=gateway, ask=session.ask,
                      calls_made=session.calls, max_calls=session.max_calls)
    denied = pre_tool_use(ctx)
    if denied:
        session.emit("tool_call", tool=name, permission=ctx.permission.value, decision="denied", reason=denied)
        return denied
    _, params = gateway.describe(name)
    kwargs = {"incident_id": incident.incident_id} if "incident_id" in params else {}
    if "query" in params:
        kwargs["query"] = " ".join(str(arguments.get("query") or default_query).split())[:200]
    session.emit("tool_call", tool=name, permission=ctx.permission.value, decision="allowed",
                 requested=arguments, used=kwargs)
    session.calls += 1
    try:
        result = gateway.invoke(name, **kwargs)
    except ToolArgumentError:
        session.emit("tool_result", tool=name, status="error", reason="invalid argument")
        return "error: invalid query (use plain keywords)"
    except ToolNotAllowedError:
        session.calls = session.max_calls
        session.emit("tool_result", tool=name, status="error", reason="budget exhausted")
        return "error: tool budget exhausted; answer now"
    withheld = post_tool_use(ctx, result)
    if withheld:
        session.emit("tool_result", tool=name, status="withheld", reason=withheld)
        return f"error: result withheld ({withheld})"
    session.emit("tool_result", tool=name, status=result.status.value, source=result.source,
                 ref=f"T{len(session.fetched) + 1}" if result.status is ToolStatus.OK and "query" not in params else None)
    if "query" in params:
        session.knowledge.append(result)
        return f"{name} (reference material, not evidence): " + json.dumps(result.data)[:MAX_RESULT_CHARS]
    if result.status is not ToolStatus.OK:
        session.no_data.append(name)
        return f"{name}: no_data (nothing was saved for this incident)"
    ref = f"T{len(session.fetched) + 1}"
    session.fetched[ref] = result
    return f"{ref} = {name}: " + json.dumps(result.data)[:MAX_RESULT_CHARS]
