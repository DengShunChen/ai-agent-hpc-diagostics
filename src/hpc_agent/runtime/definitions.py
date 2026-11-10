"""Agent definitions: one Markdown file per agent in config/agents/.

    ---
    name: storage-agent
    description: "..."
    domain: storage
    tools: [get_ost_usage, ...]        # can only narrow the gateway allowlist
    llm:                               # only for agents that can reason with a model
      categories: [storage.capacity, ...]
      max_rounds: 4
      max_tool_calls: 6
    ---
    <system prompt, a str.format template>

Like Claude Code's agent files: identity, tools and limits are configuration; the harness
(runtime/loop.py) is shared. Files are parsed with yaml.safe_load; anything malformed or
unknown fails closed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hpc_agent.data_adapter import default_data_root

_NAME = r"^[a-z][a-z0-9-]*-(agent|coordinator)$"
_CATEGORY = r"^[a-z]+(\.[a-z_]+)+$"
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class DefinitionError(ValueError):
    pass


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: tuple[str, ...] = ()
    max_rounds: int = Field(default=4, ge=1, le=10)
    max_tool_calls: int = Field(default=6, ge=0, le=20)


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME)
    description: str = Field(min_length=1, max_length=300)
    domain: str = Field(pattern=r"^[a-z-]+$")
    tools: tuple[str, ...] = ()
    llm: LLMSettings | None = None
    prompt: str = ""

    def render(self, **values: str) -> str:
        """The system prompt with its placeholders filled in."""
        if self.llm is None or not self.prompt:
            raise DefinitionError(f"{self.name} has no LLM prompt")
        return self.prompt.format(**values)


def parse_definition(text: str, source: str) -> AgentDefinition:
    match = _FRONTMATTER.match(text)
    if not match:
        raise DefinitionError(f"{source}: expected YAML frontmatter between '---' lines")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise DefinitionError(f"{source}: frontmatter must be a mapping")
    try:
        definition = AgentDefinition.model_validate({**data, "prompt": match.group(2).rstrip("\n")})
    except ValidationError as error:
        raise DefinitionError(f"{source}: {error.error_count()} problem(s) in the definition") from None
    if definition.llm and any(not re.match(_CATEGORY, c) for c in definition.llm.categories):
        raise DefinitionError(f"{source}: invalid category name")
    if definition.prompt and definition.llm is None:
        raise DefinitionError(f"{source}: a prompt needs an 'llm' section")
    return definition


def load_definitions(root: Path | None = None) -> dict[str, AgentDefinition]:
    directory = (root or default_data_root()) / "config" / "agents"
    definitions: dict[str, AgentDefinition] = {}
    for path in sorted(directory.glob("*.md")):
        definition = parse_definition(path.read_text(encoding="utf-8"), path.name)
        if definition.name != path.stem:
            raise DefinitionError(f"{path.name} declares name {definition.name!r}")
        definitions[definition.name] = definition
    if not definitions:
        raise DefinitionError(f"no agent definitions in {directory}")
    return definitions
