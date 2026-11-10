"""Permissions and hooks around every tool call a model asks for.

Like Claude Code's permission levels and PreToolUse / PostToolUse hooks, but the hooks are
fixed Python functions registered here: configuration can't add a hook, and no hook runs a
command.

- Permission: `deny` (not in the agent's scope), `ask` (listed under `ask:` in the agent's
  definition: needs a human, so an unattended diagnosis never runs it), `allow` (the rest).
- PreToolUse hooks decide before a call; the first one that objects wins.
- PostToolUse hooks may withhold a result from the model (it is then not usable as evidence).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from hpc_agent.safety.secrets import SENSITIVE_NOTICE, find_credential_like
from hpc_agent.tools.gateway import ScopedGateway, ToolResult


class Permission(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class CallContext:
    """What the hooks know about one requested call."""

    agent: str
    tool: str
    gateway: ScopedGateway
    ask: frozenset[str]
    calls_made: int
    max_calls: int
    extra: dict = field(default_factory=dict)

    @property
    def permission(self) -> Permission:
        if self.tool not in self.gateway.allowed_tools:
            return Permission.DENY
        return Permission.ASK if self.tool in self.ask else Permission.ALLOW


PreHook = Callable[[CallContext], str | None]  # returns the message the model sees when it objects
PostHook = Callable[[CallContext, ToolResult], str | None]  # returns why the result is withheld


def scope(ctx: CallContext) -> str | None:
    return f"error: {ctx.tool} is not one of your tools" if ctx.permission is Permission.DENY else None


def needs_human(ctx: CallContext) -> str | None:
    if ctx.permission is Permission.ASK:
        return f"error: {ctx.tool} needs a human's approval and is not run during an unattended diagnosis"
    return None


def call_limit(ctx: CallContext) -> str | None:
    return "error: tool call limit reached; answer now" if ctx.calls_made >= ctx.max_calls else None


def withhold_sensitive(ctx: CallContext, result: ToolResult) -> str | None:
    return SENSITIVE_NOTICE if find_credential_like(result.data) else None


PRE_TOOL_USE: tuple[PreHook, ...] = (scope, needs_human, call_limit)
POST_TOOL_USE: tuple[PostHook, ...] = (withhold_sensitive,)


def pre_tool_use(ctx: CallContext) -> str | None:
    for hook in PRE_TOOL_USE:
        message = hook(ctx)
        if message:
            return message
    return None


def post_tool_use(ctx: CallContext, result: ToolResult) -> str | None:
    for hook in POST_TOOL_USE:
        reason = hook(ctx, result)
        if reason:
            return reason
    return None
