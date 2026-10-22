"""De-identification of site exports.

Host, job and filesystem names are replaced by keyed pseudonyms (HMAC-SHA256). The same
name gets the same pseudonym across an incident and all its datasets, so correlation still
works, but the original can't be recovered without the key. The key comes from the
operator's environment; it is never written to disk and never logged.

After pseudonymization every string is scanned. IP addresses, e-mail addresses, fully
qualified domain names and home-directory paths cause the incident to be rejected, not
"fixed": a pattern that slipped through means the export needs a human look.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

KEY_ENV = "HPC_AGENT_PSEUDONYM_KEY"
MIN_KEY_LENGTH = 32

# JSON keys whose values are identifiers, and the pseudonym prefix for each.
IDENTIFIER_KEYS = {"node": "node", "allocated_nodes": "node", "job_id": "job", "filesystem": "fs"}

_FORBIDDEN = {
    "IPv4 address": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "IPv6 address": re.compile(
        r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}|(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4})?)(?![\w:])",
        re.IGNORECASE,
    ),
    "e-mail address": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "domain name": re.compile(
        r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
        r"\.(?:com|net|org|edu|gov|jp|tw|kr|cn|hk|sg|local|lan|internal|intra|corp|io)\b",
        re.IGNORECASE,
    ),
    "home-directory path": re.compile(r"/(?:home|Users|export/home)/[^/\s]+"),
}


class DeidentificationError(ValueError):
    """An export still contains identifying data. The message never contains the value."""


class Pseudonymizer:
    def __init__(self, key: str) -> None:
        if not isinstance(key, str) or len(key) < MIN_KEY_LENGTH:
            raise DeidentificationError(f"pseudonymization key must be at least {MIN_KEY_LENGTH} characters")
        self._key = key.encode("utf-8")
        self._mapped: dict[str, str] = {}

    @classmethod
    def from_env(cls) -> Pseudonymizer:
        key = os.environ.get(KEY_ENV)
        if not key:
            raise DeidentificationError(f"set {KEY_ENV} (at least {MIN_KEY_LENGTH} characters, kept outside the repo)")
        return cls(key)

    def pseudonym(self, kind: str, value: str) -> str:
        digest = hmac.new(self._key, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()[:12]
        pseudonym = f"{kind}-{digest}"
        self._mapped[value] = pseudonym
        return pseudonym

    def structured(self, data: Any) -> Any:
        """Replace the values of identifier keys (see IDENTIFIER_KEYS), recursively."""
        if isinstance(data, dict):
            out = {}
            for key, value in data.items():
                kind = IDENTIFIER_KEYS.get(key)
                if kind and isinstance(value, str):
                    out[key] = self.pseudonym(kind, value)
                elif kind and isinstance(value, list):
                    out[key] = [self.pseudonym(kind, v) if isinstance(v, str) else v for v in value]
                else:
                    out[key] = self.structured(value)
            return out
        if isinstance(data, list):
            return [self.structured(item) for item in data]
        return data

    def free_text(self, data: Any) -> Any:
        """Replace every identifier already seen in structured fields wherever it appears in text."""
        if isinstance(data, dict):
            return {key: self.free_text(value) for key, value in data.items()}
        if isinstance(data, list):
            return [self.free_text(item) for item in data]
        if isinstance(data, str):
            for original in sorted(self._mapped, key=len, reverse=True):
                data = re.sub(rf"(?<![\w-]){re.escape(original)}(?![\w-])", self._mapped[original], data)
        return data


def find_identifiers(data: Any, path: str = "$") -> list[str]:
    """Locations (and the kind of identifier) of strings that still look identifying."""
    hits: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            hits.extend(find_identifiers(value, f"{path}.{key}"))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            hits.extend(find_identifiers(item, f"{path}[{i}]"))
    elif isinstance(data, str):
        hits.extend(f"{path} ({kind})" for kind, pattern in _FORBIDDEN.items() if pattern.search(data))
    return hits


def assert_deidentified(data: Any, context: str) -> None:
    hits = find_identifiers(data)
    if hits:
        raise DeidentificationError(f"identifying data left in {context}: {hits}")
