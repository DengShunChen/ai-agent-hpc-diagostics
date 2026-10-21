"""Scheduler tools (read saved synthetic data only)."""

from __future__ import annotations

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.tools.gateway import ToolSpec, saved_data_tool


def tool_specs(adapter: ReadOnlyDataAdapter) -> list[ToolSpec]:
    return [
        saved_data_tool(adapter, "get_job_status", "Saved scheduler job status", "scheduler", "job_status"),
        saved_data_tool(adapter, "get_node_status", "Saved scheduler node state", "scheduler", "node_status"),
    ]
