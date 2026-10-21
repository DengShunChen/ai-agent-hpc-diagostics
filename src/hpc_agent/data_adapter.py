"""Read-only test-data adapter.

The only component that touches the filesystem for operational data. Reads are
confined to fixtures/, test_data/ and knowledge/ under the data root; every file is
size-limited, parsed as JSON and scanned for credential-like content before use.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from hpc_agent.models.incident import INCIDENT_ID_PATTERN, Incident
from hpc_agent.safety.secrets import assert_no_credentials

ALLOWED_DIRS = ("fixtures", "test_data", "knowledge")
MAX_FILE_BYTES = 1_000_000
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DataAccessError(PermissionError):
    """A read outside the allowed data roots, or of an invalid file."""


def default_data_root() -> Path:
    override = os.environ.get("HPC_AGENT_DATA_ROOT")
    return Path(override) if override else Path(__file__).resolve().parents[2]


class ReadOnlyDataAdapter:
    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or default_data_root()).resolve()
        self._allowed = tuple((self._root / d).resolve() for d in ALLOWED_DIRS)

    def _resolve(self, *segments: str) -> Path:
        for segment in segments:
            if not isinstance(segment, str) or not _SAFE_SEGMENT.match(segment) or ".." in segment:
                raise DataAccessError(f"invalid path segment: {segment!r}")
        path = self._root.joinpath(*segments).resolve()  # follows symlinks before the check
        if not any(path.is_relative_to(base) for base in self._allowed):
            raise DataAccessError("path outside allowed read-only data roots")
        return path

    def exists(self, *segments: str) -> bool:
        return self._resolve(*segments).is_file()

    def read_json(self, *segments: str) -> Any:
        path = self._resolve(*segments)
        if not path.is_file():
            raise FileNotFoundError("/".join(segments))
        if path.stat().st_size > MAX_FILE_BYTES:
            raise DataAccessError(f"file exceeds {MAX_FILE_BYTES} bytes: {'/'.join(segments)}")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert_no_credentials(data, context="/".join(segments))
        return data

    def read_text(self, *segments: str) -> str:
        """A Markdown document from knowledge/, size-limited and credential-scanned."""
        if not segments or segments[0] != "knowledge" or not segments[-1].endswith(".md"):
            raise DataAccessError("only knowledge/*.md documents can be read as text")
        path = self._resolve(*segments)
        if not path.is_file():
            raise FileNotFoundError("/".join(segments))
        if path.stat().st_size > MAX_FILE_BYTES:
            raise DataAccessError(f"file exceeds {MAX_FILE_BYTES} bytes: {'/'.join(segments)}")
        text = path.read_text(encoding="utf-8")
        assert_no_credentials(text.splitlines(), context="/".join(segments))
        return text

    def list_files(self, *segments: str, suffix: str) -> list[str]:
        directory = self._resolve(*segments)
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix == suffix)

    def load_incident(self, incident_id: str) -> Incident:
        if not re.match(INCIDENT_ID_PATTERN, incident_id or ""):
            raise DataAccessError(f"invalid incident id: {incident_id!r}")
        incident = Incident.model_validate(
            self.read_json("fixtures", "incidents", f"{incident_id}.json")
        )
        if incident.incident_id != incident_id:
            raise DataAccessError("fixture incident_id does not match its file name")
        return incident

    def list_incident_ids(self) -> list[str]:
        directory = self._resolve("fixtures", "incidents")
        return sorted(p.stem for p in directory.glob("*.json"))
