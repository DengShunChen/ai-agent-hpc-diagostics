"""Network tools (read saved synthetic data only)."""

from __future__ import annotations

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.tools.gateway import ToolSpec, saved_data_tool


def tool_specs(adapter: ReadOnlyDataAdapter) -> list[ToolSpec]:
    return [
        saved_data_tool(adapter, "get_network_status", "Saved interface/link status", "network", "network_status"),
    ]
