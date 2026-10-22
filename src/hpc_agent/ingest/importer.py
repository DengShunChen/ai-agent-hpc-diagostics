"""Import a site's exported data into a separate read-only data root.

    exported files --SiteExportAdapter (parse only)--> ExportBundle
        --contract validation, pseudonymization, identifier and credential scans-->
    <output>/fixtures/incidents/<id>.json and <output>/test_data/<domain>/<id>/<dataset>.json

Adapters only parse files a person has already exported. They never connect to a system,
run a command or call a site API; getting the export is a human, site-approved step.
An incident that fails any check is rejected as a whole and reported without its values;
nothing is repaired or guessed. The output root must be outside the repository so real data
can't be committed, and existing files are never overwritten.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from hpc_agent.ingest.contract import DATASETS
from hpc_agent.ingest.deidentify import DeidentificationError, Pseudonymizer, assert_deidentified
from hpc_agent.models.incident import Incident
from hpc_agent.safety.secrets import SensitiveDataError, assert_no_credentials

FREE_TEXT_FIELDS = ("symptoms", "alerts", "logs", "operator_notes")


class ExportImportError(ValueError):
    """A whole import refused (bad output location, unreadable export)."""


@dataclass(frozen=True)
class ExportBundle:
    """One incident as an adapter parsed it: raw JSON-like values, not yet trusted."""

    incident: dict[str, Any]
    datasets: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


class SiteExportAdapter(Protocol):
    """What a site adapter implements. Read already-exported files; nothing else."""

    name: str

    def incident_ids(self) -> list[str]: ...

    def load(self, incident_id: str) -> ExportBundle: ...


@dataclass
class ImportReport:
    adapter: str
    dry_run: bool
    imported: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    free_text_items: int = 0

    def render(self) -> str:
        verb = "would import" if self.dry_run else "imported"
        lines = [f"adapter: {self.adapter}", f"{verb}: {len(self.imported)} incident(s)"]
        lines += [f"  - {incident_id}" for incident_id in self.imported]
        if self.rejected:
            lines.append(f"rejected: {len(self.rejected)} incident(s) (not repaired)")
            lines += [f"  - {incident_id}: {reason}" for incident_id, reason in self.rejected.items()]
        if self.free_text_items:
            lines.append(f"free text: {self.free_text_items} item(s) (symptoms, alerts, logs, notes). "
                         "Known host/job/filesystem names were pseudonymized, but other personal or "
                         "customer details can't be detected automatically: review them before use.")
        return "\n".join(lines)


def _validation_summary(error: ValidationError) -> str:
    """Field locations and error types only; the offending values are never echoed."""
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '$'}: {e['type']}" for e in error.errors()[:10])


def prepare(bundle: ExportBundle, pseudonymizer: Pseudonymizer) -> dict[str, Any]:
    """Validate, pseudonymize and scan one bundle. Returns {relative path: JSON payload}."""
    try:
        incident = Incident.model_validate(bundle.incident)
    except ValidationError as error:
        raise ValueError(f"incident: {_validation_summary(error)}") from None
    payloads: dict[tuple[str, ...], Any] = {
        ("fixtures", "incidents", f"{incident.incident_id}.json"): incident.model_dump(mode="json", exclude_none=True)
    }
    for key, raw in bundle.datasets.items():
        contract = DATASETS.get(key)
        if contract is None:
            raise ValueError(f"unknown dataset {'/'.join(key)}")
        try:
            data = contract.model_validate(raw).model_dump(mode="json", exclude_none=True)
        except ValidationError as error:
            raise ValueError(f"{'/'.join(key)}: {_validation_summary(error)}") from None
        payloads[("test_data", *key[:1], incident.incident_id, f"{key[1]}.json")] = data

    # Identifiers first (so every name is known), then free text everywhere.
    payloads = {path: pseudonymizer.structured(data) for path, data in payloads.items()}
    payloads = {path: pseudonymizer.free_text(data) for path, data in payloads.items()}
    out = {}
    for path, data in payloads.items():
        context = "/".join(path)
        assert_no_credentials(data, context=context)
        assert_deidentified(data, context=context)
        out["/".join(path)] = data
    Incident.model_validate(out[f"fixtures/incidents/{incident.incident_id}.json"])  # still a valid incident
    return out


def _write(output: Path, relative: str, data: Any) -> None:
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def import_export(
    adapter: SiteExportAdapter,
    output: Path,
    pseudonymizer: Pseudonymizer,
    forbidden_root: Path | None = None,
    dry_run: bool = False,
) -> ImportReport:
    output = output.resolve()
    if forbidden_root is not None and output.is_relative_to(forbidden_root.resolve()):
        raise ExportImportError("the output data root must be outside the repository (real data must not be committed)")
    report = ImportReport(adapter=adapter.name, dry_run=dry_run)
    for incident_id in adapter.incident_ids():
        try:
            bundle = adapter.load(incident_id)
            if bundle.incident.get("incident_id") != incident_id:
                raise ValueError("incident_id in the export does not match the requested incident")
            prepared = prepare(bundle, pseudonymizer)
            existing = [rel for rel in prepared if (output / rel).exists()]
            if existing:
                raise ValueError(f"already imported ({len(existing)} file(s) exist; nothing overwritten)")
        except SensitiveDataError:
            report.rejected[incident_id] = "credential-like content (SENSITIVE INFORMATION DETECTED AND NOT PROCESSED.)"
            continue
        except (DeidentificationError, ValueError) as error:
            report.rejected[incident_id] = str(error)
            continue
        incident = prepared[f"fixtures/incidents/{incident_id}.json"]
        report.free_text_items += sum(len(incident.get(name, [])) for name in FREE_TEXT_FIELDS)
        if not dry_run:
            for relative, data in prepared.items():
                _write(output, relative, data)
        report.imported.append(incident_id)
    return report
