"""Append-only, hash-chained JSON Lines logs.

Each line is {"seq", "prev_hash", "hash", "record"} where
hash = sha256(prev_hash + "\\n" + canonical JSON of record). Editing, reordering or
deleting any earlier line breaks the chain and is reported by verify(). Removing lines
from the end cannot be detected from the file alone; anchor the latest hash externally
if that matters.

Writes are serialized with an exclusive file lock and fsync'd. Nothing here ever
rewrites or truncates an existing file.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


class AuditIntegrityError(RuntimeError):
    pass


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(prev_hash: str, record: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash}\n{_canonical(record)}".encode()).hexdigest()


class AppendOnlyLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict[str, Any]) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            last = None
            for line in handle:
                if line.strip():
                    last = line
            previous = json.loads(last) if last else None
            prev_hash = previous["hash"] if previous else GENESIS
            entry = {
                "seq": previous["seq"] + 1 if previous else 0,
                "prev_hash": prev_hash,
                "hash": _digest(prev_hash, record),
                "record": record,
            }
            handle.write(_canonical(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return entry["hash"]

    def records(self) -> list[dict[str, Any]]:
        """All records, after verifying the whole chain."""
        if not self.path.exists():
            return []
        records = []
        prev_hash = GENESIS
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    valid = (
                        entry["seq"] == len(records)
                        and entry["prev_hash"] == prev_hash
                        and entry["hash"] == _digest(prev_hash, entry["record"])
                    )
                except (ValueError, KeyError, TypeError):
                    valid = False
                if not valid:
                    raise AuditIntegrityError(f"{self.path.name}: chain broken at line {number}")
                prev_hash = entry["hash"]
                records.append(entry["record"])
        return records

    def verify(self) -> int:
        return len(self.records())
