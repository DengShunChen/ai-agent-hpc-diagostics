"""Operator authentication for the HTTP API.

Operators present a bearer token; the operator identity is derived from the token, never
asserted by the client. Only SHA-256 hashes of tokens are stored, in a JSON file that
must live outside the repository and must not be readable by group or others:

    {"operators": {"ops.wang": "sha256:<64 hex chars>"}}

No file configured, or any problem with it, means no one can authenticate (fail closed).
Tokens are 256-bit random values, so a plain SHA-256 is sufficient for storage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from pathlib import Path

from hpc_agent.models.approval import is_human_operator

_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class OperatorAuthError(PermissionError):
    pass


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


class OperatorTokens:
    def __init__(self, hashes: dict[str, str]) -> None:
        for operator, digest in hashes.items():
            if not is_human_operator(operator):
                raise OperatorAuthError(f"not a human operator identity: {operator!r}")
            if not _HASH.fullmatch(digest):
                raise OperatorAuthError(f"malformed token hash for {operator!r}")
        self._hashes = dict(hashes)

    @classmethod
    def from_file(cls, path: Path, forbidden_root: Path | None = None) -> OperatorTokens:
        resolved = path.resolve()
        if forbidden_root is not None and resolved.is_relative_to(forbidden_root.resolve()):
            raise OperatorAuthError("operator token file must be stored outside the repository")
        mode = resolved.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise OperatorAuthError("operator token file must not be accessible by group or others (chmod 600)")
        data = json.loads(resolved.read_text(encoding="utf-8"))
        operators = data.get("operators") if isinstance(data, dict) else None
        if not isinstance(operators, dict) or not all(isinstance(v, str) for v in operators.values()):
            raise OperatorAuthError("operator token file must contain an 'operators' mapping")
        return cls(operators)

    def authenticate(self, token: str | None) -> str:
        """Return the operator owning the token, or raise. Compares in constant time."""
        if not token:
            raise OperatorAuthError("missing operator token")
        candidate = hash_token(token)
        match = None
        for operator, digest in self._hashes.items():
            if hmac.compare_digest(candidate, digest):
                match = operator
        if match is None:
            raise OperatorAuthError("invalid operator token")
        return match


def issue_token(path: Path, operator: str, forbidden_root: Path | None = None) -> str:
    """Create or rotate an operator's token. The plaintext is returned once and never stored."""
    if not is_human_operator(operator):
        raise OperatorAuthError(f"not a human operator identity: {operator!r}")
    if forbidden_root is not None and path.resolve().is_relative_to(forbidden_root.resolve()):
        raise OperatorAuthError("operator token file must be stored outside the repository")
    hashes = OperatorTokens.from_file(path)._hashes if path.exists() else {}
    token = secrets.token_urlsafe(32)
    hashes[operator] = hash_token(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"operators": hashes}, handle, indent=2)
    os.replace(tmp, path)
    return token
