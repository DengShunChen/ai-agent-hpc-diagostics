"""Import safety: what a site export may not carry into the data root, and what the importer may not do."""

import re
import stat

import pytest

from hpc_agent.ingest.csv_export import CsvExportAdapter
from hpc_agent.ingest.deidentify import Pseudonymizer
from hpc_agent.ingest.importer import ExportBundle, ExportImportError, import_export
from tests.conftest import PROJECT_ROOT

KEY = "safety-test-key-0123456789-0123456789"
HEADER = "incident_id,timestamp,severity,system,node,job_id,symptoms,alerts,logs,operator_notes\n"


def export(tmp_path, incidents_row, **files):
    directory = tmp_path / "export"
    directory.mkdir()
    (directory / "incidents.csv").write_text(HEADER + incidents_row + "\n", encoding="utf-8")
    for name, content in files.items():
        (directory / f"{name}.csv").write_text(content, encoding="utf-8")
    return directory


def run(directory, tmp_path):
    return import_export(CsvExportAdapter(directory), tmp_path / "out", Pseudonymizer(KEY))


@pytest.mark.parametrize("log, value, kind", [
    ("connection reset by 10.20.30.40", "10.20.30.40", "IPv4 address"),
    ("peer fe80::1a2b lost", "fe80::1a2b", "IPv6 address"),
    ("mail sent to someone@example.com", "someone", "e-mail address"),
    ("cannot resolve storage01.corp.example.jp", "storage01", "domain name"),
    ("core dumped in /home/alice/run", "alice", "home-directory path"),
])
def test_identifying_free_text_rejects_the_incident(tmp_path, log, value, kind):
    report = run(export(tmp_path, f"SIM-X-001,,high,HPC,sim-node-1,,,,{log},"), tmp_path)
    assert report.imported == [] and kind in report.rejected["SIM-X-001"]
    assert value not in report.render()  # the value itself is never echoed
    assert not (tmp_path / "out").exists()


def test_credentials_reject_the_incident_without_echoing_them(tmp_path):
    report = run(export(tmp_path, "SIM-X-002,,high,HPC,sim-node-1,,,,login failed password=hunter2,"), tmp_path)
    assert "SENSITIVE INFORMATION DETECTED AND NOT PROCESSED." in report.rejected["SIM-X-002"]
    assert "hunter2" not in report.render()


def test_invalid_values_are_reported_by_location_not_value(tmp_path):
    directory = export(tmp_path, "SIM-X-003,,high,HPC,sim-node-1,,,,,",
                       ost_usage="incident_id,ost,used_pct,state,collected_at\nSIM-X-003,OST0000,lots-and-lots,active,\n")
    report = run(directory, tmp_path)
    assert "osts.0.used_pct" in report.rejected["SIM-X-003"]
    assert "lots-and-lots" not in report.render()


@pytest.mark.parametrize("value", ["2026-01-15T03:12:00", "yesterday"])
def test_timestamps_need_a_timezone(tmp_path, value):
    report = run(export(tmp_path, f"SIM-X-004,{value},high,HPC,sim-node-1,,,,,"), tmp_path)
    assert "timestamp" in report.rejected["SIM-X-004"]


def test_unreviewed_columns_are_refused(tmp_path):
    directory = export(tmp_path, "SIM-X-005,,high,HPC,sim-node-1,,,,,",
                       node_status="incident_id,node,state,owner_email\nSIM-X-005,sim-node-1,DRAIN,x\n")
    with pytest.raises(ExportImportError, match="unexpected or missing columns"):
        CsvExportAdapter(directory)


def test_rows_for_unknown_incidents_are_refused(tmp_path):
    directory = export(tmp_path, "SIM-X-006,,high,HPC,sim-node-1,,,,,",
                       node_status="incident_id,node,state\nSIM-OTHER-001,sim-node-1,DRAIN\n")
    with pytest.raises(ExportImportError, match="missing from incidents.csv"):
        CsvExportAdapter(directory)


class _Adapter:
    name = "test"

    def __init__(self, bundle):
        self.bundle = bundle

    def incident_ids(self):
        return [self.bundle.incident["incident_id"]]

    def load(self, incident_id):
        return self.bundle


@pytest.mark.parametrize("datasets, expected", [
    ({("gpu", "gpu_health"): {"node": "n1", "gpus": [{"index": 0, "serial": "X"}]}}, "extra_forbidden"),
    ({("gpu", "dcgm_diag"): {"node": "n1"}}, "unknown dataset"),
    ({("scheduler", "job_status"): {"job_id": "j1", "exit_code": "137; rm -rf /"}}, "int_parsing"),
])
def test_bundles_outside_the_contract_are_rejected(tmp_path, datasets, expected):
    bundle = ExportBundle({"incident_id": "SIM-X-007", "severity": "low"}, datasets)
    report = import_export(_Adapter(bundle), tmp_path / "out", Pseudonymizer(KEY))
    assert expected in report.rejected["SIM-X-007"]


def test_bundle_must_match_the_requested_incident(tmp_path):
    class Mismatch(_Adapter):
        def incident_ids(self):
            return ["SIM-X-008"]

    report = import_export(Mismatch(ExportBundle({"incident_id": "SIM-X-009", "severity": "low"})),
                           tmp_path / "out", Pseudonymizer(KEY))
    assert "does not match" in report.rejected["SIM-X-008"]


def test_output_files_are_private(tmp_path):
    run(export(tmp_path, "SIM-X-010,,high,HPC,sim-node-1,,,,,"), tmp_path)
    paths = list((tmp_path / "out").rglob("*.json"))
    assert len(paths) == 2  # the incident and the DATA_CLASS.json marker
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in paths)


def test_key_never_appears_in_output_or_report(tmp_path):
    report = run(export(tmp_path, "SIM-X-011,,high,HPC,sim-node-1,,,,,"), tmp_path)
    text = report.render() + "".join(p.read_text() for p in (tmp_path / "out").rglob("*.json"))
    assert KEY not in text


INGEST_SOURCES = sorted((PROJECT_ROOT / "src" / "hpc_agent" / "ingest").glob("*.py"))


@pytest.mark.parametrize("path", INGEST_SOURCES, ids=lambda p: p.name)
def test_ingest_code_has_no_network_access(path):
    text = path.read_text(encoding="utf-8")
    pattern = r"^\s*(import|from)\s+(socket|http|urllib|requests|httpx|ssl|ftplib|smtplib|paramiko)\b"
    assert not re.search(pattern, text, re.MULTILINE)


def test_example_export_is_synthetic():
    text = "".join(p.read_text(encoding="utf-8") for p in (PROJECT_ROOT / "examples" / "site-export").glob("*.csv"))
    hosts = set(re.findall(r"\b[a-z][\w-]*-node-\d+\b", text))
    assert hosts and all(h.startswith("sim-") for h in hosts)
