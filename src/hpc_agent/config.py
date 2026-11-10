"""Configuration loading.

Agent tool scopes come from the agent definitions (config/agents/<name>.md). A scope can only
*narrow* which registered gateway tools an agent may call; it cannot add tools to the gateway
allowlist. Malformed config fails closed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from hpc_agent.data_adapter import default_data_root
from hpc_agent.models.site import SITE_ID_PATTERN, SiteProfile
from hpc_agent.runtime.definitions import DefinitionError, load_definitions


class ConfigError(ValueError):
    pass


def load_agent_tool_scopes(root: Path | None = None) -> dict[str, frozenset[str]]:
    try:
        definitions = load_definitions(root)
    except (DefinitionError, yaml.YAMLError) as error:
        raise ConfigError(str(error)) from None
    return {name: frozenset(d.tools) for name, d in definitions.items()}


def load_site_profile(site_id: str | None = None, root: Path | None = None) -> SiteProfile:
    """The profile in config/sites/<site_id>.yaml (default: $HPC_AGENT_SITE or "default")."""
    site_id = site_id or os.environ.get("HPC_AGENT_SITE") or "default"
    if not re.match(SITE_ID_PATTERN, site_id):
        raise ConfigError(f"invalid site id: {site_id!r}")
    path = (root or default_data_root()) / "config" / "sites" / f"{site_id}.yaml"
    if not path.is_file():
        raise ConfigError(f"no site profile for {site_id!r} (expected {path.name} in config/sites/)")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        profile = SiteProfile.model_validate(data)
    except ValidationError as error:
        raise ConfigError(f"invalid site profile {path.name}: {error.error_count()} problem(s)") from None
    if profile.site_id != site_id:
        raise ConfigError(f"{path.name} declares site_id {profile.site_id!r}")
    return profile
