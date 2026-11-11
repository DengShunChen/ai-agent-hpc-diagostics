"""Skills: diagnostic procedures an agent can read on demand (Agent Skills style SKILL.md files).

    skills/<domain>/<name>/SKILL.md
    ---
    name: job-termination
    description: "How to read exit codes, signals and walltime ..."   # shown in the prompt
    agents: [scheduler-agent]                                          # who sees it in their index
    ---
    <the procedure>

Only the index (name + description) goes into an agent's prompt; the model reads the full text
with the `load_skill` tool when it needs it. Skills live in the repository, so they are
versioned and reviewed like code. An agent can't write one directly: it may draft one from a
human-approved run (llm/skill_writer.py), and only a human operator's approval installs it. They are reference material, never
evidence, and they can't change the agent's rules. `version()` fingerprints all skills so an
evaluation can record exactly which ones it ran with.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from hpc_agent.safety.secrets import assert_no_credentials

SKILL_NAME = r"^[a-z][a-z0-9-]{1,63}$"
MAX_BODY_CHARS = 8000
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_AGENT = re.compile(r"^[a-z][a-z0-9-]*-agent$")


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    agents: tuple[str, ...]
    body: str
    path: str  # relative to the data root, e.g. skills/gpu/xid-triage/SKILL.md
    sha256: str


def parse_skill(text: str, path: str) -> Skill:
    match = _FRONTMATTER.match(text)
    if not match:
        raise SkillError(f"{path}: expected YAML frontmatter between '---' lines")
    meta = yaml.safe_load(match.group(1))
    if not isinstance(meta, dict) or set(meta) - {"name", "description", "agents"}:
        raise SkillError(f"{path}: frontmatter may only hold name, description and agents")
    name, description, agents = meta.get("name"), meta.get("description"), meta.get("agents")
    body = match.group(2).strip()
    if not isinstance(name, str) or not re.match(SKILL_NAME, name):
        raise SkillError(f"{path}: invalid skill name")
    if not isinstance(description, str) or not 1 <= len(description) <= 200:
        raise SkillError(f"{path}: description must be 1-200 characters")
    if not isinstance(agents, list) or not agents or not all(isinstance(a, str) and _AGENT.match(a) for a in agents):
        raise SkillError(f"{path}: agents must be a non-empty list of agent names")
    if not body or len(body) > MAX_BODY_CHARS:
        raise SkillError(f"{path}: body must be 1-{MAX_BODY_CHARS} characters")
    assert_no_credentials([description, *body.splitlines()], context=path)
    return Skill(name, description, tuple(agents), body, path, hashlib.sha256(text.encode()).hexdigest())


class SkillLibrary:
    def __init__(self, skills: dict[str, Skill]) -> None:
        self._skills = skills

    @classmethod
    def load(cls, root: Path) -> SkillLibrary:
        """All skills under <root>/skills; an absent directory means no skills."""
        directory = root / "skills"
        skills: dict[str, Skill] = {}
        for path in sorted(directory.glob("*/*/SKILL.md")) if directory.is_dir() else []:
            relative = path.relative_to(root).as_posix()
            skill = parse_skill(path.read_text(encoding="utf-8"), relative)
            if skill.name != path.parent.name or skill.name in skills:
                raise SkillError(f"{relative}: name must match its directory and be unique")
            skills[skill.name] = skill
        return cls(skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def for_agent(self, agent: str) -> list[Skill]:
        return [s for s in self._skills.values() if agent in s.agents]

    def version(self) -> str:
        """Fingerprint of every skill (empty library: 'none')."""
        if not self._skills:
            return "none"
        digest = hashlib.sha256("".join(f"{s.path}:{s.sha256}\n" for s in sorted(
            self._skills.values(), key=lambda s: s.path)).encode())
        return digest.hexdigest()[:12]
