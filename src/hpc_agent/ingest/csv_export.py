"""Reference adapter: a directory of CSV files a site exported by hand or from its own tools.

Only incidents.csv is required. Each other file holds one dataset; every row carries the
incident_id it belongs to. Empty cells mean "not exported" and stay missing.

    incidents.csv           incident_id,timestamp,severity,system,node,job_id,symptoms,alerts,logs,operator_notes
    filesystem_usage.csv    incident_id,filesystem,used_pct,health,collected_at        (one row per incident)
    ost_usage.csv           incident_id,ost,used_pct,state,collected_at                (one row per OST)
    job_status.csv          incident_id,job_id,scheduler,state,exit_code,requested_walltime,elapsed,
                            end_time,allocated_nodes,termination_reason                (one row per incident)
    node_status.csv         incident_id,node,state,reason,state_changed_at             (one row per node)
    network_status.csv      incident_id,node,interface,state,rx_errors,tx_errors,link_flaps,last_change,members
    gpu_health.csv          incident_id,node,index,health,xid_events,temperature_c,power_w,
                            ecc_dbe_volatile,pcie_replay_count,nvlink_errors           (one row per GPU)

List cells: symptoms/alerts/logs/operator_notes and allocated_nodes use " | " between items;
members is "eth0=up;eth1=down"; xid_events is "79x1@2026-02-03T21:39:52Z;13x2" (time optional).
Columns outside these lists are refused, because nobody reviewed what they might contain.
Type checking happens in the contract, so values are passed on as strings.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from hpc_agent.ingest.importer import ExportBundle, ExportImportError

MAX_FILE_BYTES = 20_000_000
LIST_SEPARATOR = "|"

COLUMNS = {
    "incidents": ("incident_id", "timestamp", "severity", "system", "node", "job_id",
                  "symptoms", "alerts", "logs", "operator_notes"),
    "filesystem_usage": ("incident_id", "filesystem", "used_pct", "health", "collected_at"),
    "ost_usage": ("incident_id", "ost", "used_pct", "state", "collected_at"),
    "job_status": ("incident_id", "job_id", "scheduler", "state", "exit_code", "requested_walltime",
                   "elapsed", "end_time", "allocated_nodes", "termination_reason"),
    "node_status": ("incident_id", "node", "state", "reason", "state_changed_at"),
    "network_status": ("incident_id", "node", "interface", "state", "rx_errors", "tx_errors",
                       "link_flaps", "last_change", "members"),
    "gpu_health": ("incident_id", "node", "index", "health", "xid_events", "temperature_c", "power_w",
                   "ecc_dbe_volatile", "pcie_replay_count", "nvlink_errors"),
}
DOMAIN = {"filesystem_usage": "storage", "ost_usage": "storage", "job_status": "scheduler",
          "node_status": "scheduler", "network_status": "network", "gpu_health": "gpu"}


def _cells(row: dict[str, str]) -> dict[str, str]:
    """Non-empty cells only: an empty cell is missing data, not an empty string."""
    return {key: value.strip() for key, value in row.items() if key != "incident_id" and value and value.strip()}


def _split(value: str, separator: str) -> list[str]:
    return [item.strip() for item in value.split(separator) if item.strip()]


def _single(rows: list[dict[str, str]], dataset: str) -> dict[str, str]:
    if len(rows) != 1:
        raise ValueError(f"{dataset}: expected one row per incident, found {len(rows)}")
    return rows[0]


def _shared(rows: list[dict[str, str]], column: str, dataset: str) -> dict[str, str]:
    """A per-dataset value repeated on each row (e.g. collected_at); rows must agree."""
    values = {row.pop(column) for row in rows if column in row}
    if len(values) > 1:
        raise ValueError(f"{dataset}: rows disagree on {column}")
    return {column: values.pop()} if values else {}


def _members(value: str) -> list[dict[str, str]]:
    members = []
    for item in _split(value, ";"):
        name, _, state = item.partition("=")
        members.append({"interface": name.strip(), **({"state": state.strip()} if state.strip() else {})})
    return members


def _xid_events(value: str) -> list[dict[str, str]]:
    events = []
    for item in _split(value, ";"):
        head, _, seen = item.partition("@")
        xid, _, count = head.partition("x")
        event = {"xid": xid.strip()}
        if count.strip():
            event["count"] = count.strip()
        if seen.strip():
            event["timestamp"] = seen.strip()
        events.append(event)
    return events


class CsvExportAdapter:
    name = "csv-export"

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._rows: dict[str, dict[str, list[dict[str, str]]]] = {}
        for dataset in COLUMNS:
            self._rows[dataset] = self._read(dataset)
        if not self._rows["incidents"]:
            raise ExportImportError("incidents.csv is missing or empty")
        orphans = {i for dataset, rows in self._rows.items() for i in rows} - set(self._rows["incidents"])
        if orphans:
            raise ExportImportError(f"rows reference incidents missing from incidents.csv: {len(orphans)} id(s)")

    def _read(self, dataset: str) -> dict[str, list[dict[str, str]]]:
        path = self._directory / f"{dataset}.csv"
        if not path.is_file():
            return {}
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ExportImportError(f"{path.name} exceeds {MAX_FILE_BYTES} bytes")
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            header = tuple(reader.fieldnames or ())
            unknown = set(header) - set(COLUMNS[dataset])
            if unknown or "incident_id" not in header:
                raise ExportImportError(f"{path.name}: unexpected or missing columns (allowed: {', '.join(COLUMNS[dataset])})")
            for row in reader:
                if None in row:
                    raise ExportImportError(f"{path.name}: line {reader.line_num} has more cells than the header")
                incident_id = (row.get("incident_id") or "").strip()
                if not incident_id:
                    raise ExportImportError(f"{path.name}: line {reader.line_num} has no incident_id")
                grouped[incident_id].append(_cells(row))
        return dict(grouped)

    def incident_ids(self) -> list[str]:
        return sorted(self._rows["incidents"])

    def load(self, incident_id: str) -> ExportBundle:
        incident: dict[str, Any] = dict(_single(self._rows["incidents"][incident_id], "incidents"))
        for name in ("symptoms", "alerts", "logs", "operator_notes"):
            if name in incident:
                incident[name] = _split(incident[name], LIST_SEPARATOR)
        incident["incident_id"] = incident_id

        datasets: dict[tuple[str, str], dict[str, Any]] = {}
        rows = {dataset: [dict(r) for r in self._rows[dataset].get(incident_id, [])] for dataset in DOMAIN}
        if rows["filesystem_usage"]:
            datasets[("storage", "filesystem_usage")] = _single(rows["filesystem_usage"], "filesystem_usage")
        if rows["ost_usage"]:
            shared = _shared(rows["ost_usage"], "collected_at", "ost_usage")
            datasets[("storage", "ost_usage")] = {"osts": rows["ost_usage"], **shared}
        if rows["job_status"]:
            job: dict[str, Any] = _single(rows["job_status"], "job_status")
            if "allocated_nodes" in job:
                job["allocated_nodes"] = _split(job["allocated_nodes"], LIST_SEPARATOR)
            datasets[("scheduler", "job_status")] = job
        if rows["node_status"]:
            datasets[("scheduler", "node_status")] = {"nodes": rows["node_status"]}
        if rows["network_status"]:
            interfaces: list[dict[str, Any]] = rows["network_status"]
            for entry in interfaces:
                if "members" in entry:
                    entry["members"] = _members(entry["members"])
            datasets[("network", "network_status")] = {"interfaces": interfaces}
        if rows["gpu_health"]:
            gpus: list[dict[str, Any]] = rows["gpu_health"]
            nodes = {gpu.pop("node", None) for gpu in gpus}
            if len(nodes) != 1 or None in nodes:
                raise ValueError("gpu_health: every row of an incident needs the same node")
            for gpu in gpus:
                if "xid_events" in gpu:
                    gpu["xid_events"] = _xid_events(gpu["xid_events"])
            datasets[("gpu", "gpu_health")] = {"node": nodes.pop(), "gpus": gpus}
        return ExportBundle(incident=incident, datasets=datasets)
