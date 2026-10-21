"""Credential-like content detection.

Fail-closed scanner used on fixtures, test data and audit records. It reports only
the *location* of suspicious content (a JSON-path-like string), never the value.
"""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_NOTICE = "SENSITIVE INFORMATION DETECTED AND NOT PROCESSED."

_SENSITIVE_KEY = re.compile(
    r"pass(word|wd)?|pwd|secret|token|api[_-]?key|private[_-]?key|credential|"
    r"access[_-]?key|auth[_-]?key|ssh[_-]?key",
    re.IGNORECASE,
)

_SENSITIVE_VALUES = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(sk|ghp|gho|ghs|xox[abprs])[-_][A-Za-z0-9_-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{10,}", re.IGNORECASE),
    re.compile(
        r"\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)


class SensitiveDataError(ValueError):
    """Raised when credential-like content is found. The message never contains the value."""


def find_credential_like(data: Any, path: str = "$") -> list[str]:
    """Return the locations of credential-like keys or values in a JSON-like structure."""
    hits: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{path}.{key}"
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                hits.append(child)
            hits.extend(find_credential_like(value, child))
    elif isinstance(data, (list, tuple)):
        for i, item in enumerate(data):
            hits.extend(find_credential_like(item, f"{path}[{i}]"))
    elif isinstance(data, str):
        if any(p.search(data) for p in _SENSITIVE_VALUES):
            hits.append(path)
    return hits


def assert_no_credentials(data: Any, context: str) -> None:
    hits = find_credential_like(data)
    if hits:
        raise SensitiveDataError(f"{SENSITIVE_NOTICE} context={context} locations={hits}")
