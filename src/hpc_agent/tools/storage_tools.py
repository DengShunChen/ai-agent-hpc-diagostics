"""Storage tools (read saved synthetic data only)."""

from __future__ import annotations

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.tools.gateway import ToolSpec, saved_data_tool


def tool_specs(adapter: ReadOnlyDataAdapter) -> list[ToolSpec]:
    return [
        saved_data_tool(adapter, "get_filesystem_usage", "Saved filesystem capacity/utilization", "storage", "filesystem_usage"),
        saved_data_tool(adapter, "get_ost_usage", "Saved per-OST utilization", "storage", "ost_usage"),
    ]
