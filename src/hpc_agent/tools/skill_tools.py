"""load_skill: read one skill (a diagnostic procedure) from the repository's skills/ directory.

Reference material only: a skill never counts as evidence and can't change an agent's rules.
"""

from __future__ import annotations

from hpc_agent.data_adapter import ReadOnlyDataAdapter, default_data_root
from hpc_agent.runtime.skills import SkillLibrary
from hpc_agent.safety.policy import Operation
from hpc_agent.tools.gateway import ToolResult, ToolSpec, ToolStatus


def tool_specs(adapter: ReadOnlyDataAdapter, library: SkillLibrary | None = None) -> list[ToolSpec]:
    # Skills ship with the code and config (like config/agents), not with an incident's data root.
    skills = library or SkillLibrary.load(default_data_root())

    def handler(skill: str) -> ToolResult:
        found = skills.get(skill)
        if found is None:
            return ToolResult(tool="load_skill", status=ToolStatus.NO_DATA)
        return ToolResult(tool="load_skill", status=ToolStatus.OK, source=found.path,
                          data={"name": found.name, "sha256": found.sha256, "body": found.body})

    return [ToolSpec(
        name="load_skill",
        description="Read a diagnostic procedure (skill) by name; reference material only, never evidence",
        operation=Operation.READ_KNOWLEDGE,
        parameters=frozenset({"skill"}),
        handler=handler,
    )]
