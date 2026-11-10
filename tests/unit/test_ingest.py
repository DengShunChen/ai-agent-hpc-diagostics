"""Site-export import: the CSV reference adapter, contract, pseudonymization and output."""

import json

import pytest

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.ingest.csv_export import CsvExportAdapter
from hpc_agent.ingest.deidentify import DeidentificationError, Pseudonymizer
from hpc_agent.ingest.importer import ExportImportError, import_export
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis
from tests.conftest import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "examples" / "site-export"
KEY = "unit-test-key-0123456789-0123456789"
ORIGINAL_NAMES = ("sim-node-0042", "sim-node-0043", "sim-gpu-node-0007", "sim-node-0201",
                  "SIM-JOB-20001", "SIM-JOB-30001", "SIM-JOB-50001")


@pytest.fixture
def imported(tmp_path):
    output = tmp_path / "data"
    report = import_export(CsvExportAdapter(EXAMPLE), output, Pseudonymizer(KEY), forbidden_root=PROJECT_ROOT)
    return output, report


def outcome(run):
    return run.rca.classification, sorted((c.category, c.status.value) for c in run.rca.probable_causes)


def test_example_export_imports_every_incident(imported):
    output, report = imported
    assert report.imported == ["SIM-EXPORT-001", "SIM-EXPORT-002", "SIM-EXPORT-003"]
    assert report.rejected == {}
    files = sorted(str(p.relative_to(output)) for p in output.rglob("*.json"))
    assert files == [
        "fixtures/DATA_CLASS.json",
        "fixtures/incidents/SIM-EXPORT-001.json",
        "fixtures/incidents/SIM-EXPORT-002.json",
        "fixtures/incidents/SIM-EXPORT-003.json",
        "test_data/gpu/SIM-EXPORT-002/gpu_health.json",
        "test_data/network/SIM-EXPORT-003/network_status.json",
        "test_data/scheduler/SIM-EXPORT-001/job_status.json",
        "test_data/scheduler/SIM-EXPORT-001/node_status.json",
        "test_data/scheduler/SIM-EXPORT-003/job_status.json",
    ]


def test_imported_root_is_marked_deidentified(imported):
    output, _ = imported
    assert ReadOnlyDataAdapter(output).data_class() == "deidentified-import"
    assert ReadOnlyDataAdapter(PROJECT_ROOT).data_class() == "synthetic"


def test_no_original_identifier_survives_anywhere(imported):
    output, _ = imported
    text = "".join(p.read_text(encoding="utf-8") for p in output.rglob("*.json"))
    assert not [name for name in ORIGINAL_NAMES if name in text]


def test_pseudonyms_are_consistent_across_incident_and_datasets(imported):
    output, _ = imported
    incident = json.loads((output / "fixtures/incidents/SIM-EXPORT-001.json").read_text())
    job = json.loads((output / "test_data/scheduler/SIM-EXPORT-001/job_status.json").read_text())
    nodes = json.loads((output / "test_data/scheduler/SIM-EXPORT-001/node_status.json").read_text())
    assert incident["node"].startswith("node-") and incident["job_id"].startswith("job-")
    assert incident["node"] in job["allocated_nodes"]
    assert incident["node"] == nodes["nodes"][0]["node"]
    assert job["job_id"] == incident["job_id"]
    assert incident["job_id"] in incident["logs"][0]  # free text follows the structured mapping
    assert incident["node"] in incident["alerts"][0]


def test_pseudonyms_depend_on_the_key():
    first, again, other = Pseudonymizer(KEY), Pseudonymizer(KEY), Pseudonymizer(KEY + "x")
    assert first.pseudonym("node", "sim-node-0042") == again.pseudonym("node", "sim-node-0042")
    assert first.pseudonym("node", "sim-node-0042") != other.pseudonym("node", "sim-node-0042")


@pytest.mark.parametrize("key", ["", "short-key"])
def test_short_or_missing_key_is_refused(key):
    with pytest.raises(DeidentificationError):
        Pseudonymizer(key)


def test_key_comes_only_from_the_environment(monkeypatch):
    monkeypatch.delenv("HPC_AGENT_PSEUDONYM_KEY", raising=False)
    with pytest.raises(DeidentificationError):
        Pseudonymizer.from_env()


@pytest.mark.parametrize("exported, fixture", [("SIM-EXPORT-001", "POC-SCHEDULER-001"),
                                               ("SIM-EXPORT-002", "POC-GPU-001")])
def test_imported_incident_diagnoses_like_the_original_fixture(imported, adapter, gateway, scopes, exported, fixture):
    output, _ = imported
    data = ReadOnlyDataAdapter(output)
    run = run_diagnosis(data.load_incident(exported), build_default_gateway(data), scopes)
    assert outcome(run) == outcome(run_diagnosis(adapter.load_incident(fixture), gateway, scopes))


def test_imported_network_incident_points_at_the_link(imported, scopes):
    output, _ = imported
    data = ReadOnlyDataAdapter(output)
    run = run_diagnosis(data.load_incident("SIM-EXPORT-003"), build_default_gateway(data), scopes)
    assert run.rca.probable_causes[0].category == "network.link_down"
    assert run.rca.confirmed_root_cause is None


def test_dry_run_writes_nothing(tmp_path):
    output = tmp_path / "data"
    report = import_export(CsvExportAdapter(EXAMPLE), output, Pseudonymizer(KEY), dry_run=True)
    assert len(report.imported) == 3 and "would import" in report.render()
    assert not output.exists()


def test_existing_files_are_never_overwritten(imported):
    output, _ = imported
    before = {p: p.read_bytes() for p in output.rglob("*.json")}
    again = import_export(CsvExportAdapter(EXAMPLE), output, Pseudonymizer(KEY))
    assert again.imported == [] and set(again.rejected) == {"SIM-EXPORT-001", "SIM-EXPORT-002", "SIM-EXPORT-003"}
    assert {p: p.read_bytes() for p in output.rglob("*.json")} == before


def test_output_inside_the_repository_is_refused():
    with pytest.raises(ExportImportError):
        import_export(CsvExportAdapter(EXAMPLE), PROJECT_ROOT / "imported", Pseudonymizer(KEY),
                      forbidden_root=PROJECT_ROOT)
    assert not (PROJECT_ROOT / "imported").exists()


def test_cli_import_and_dry_run(tmp_path, monkeypatch, capsys):
    from hpc_agent.main import main

    output = tmp_path / "data"
    monkeypatch.setenv("HPC_AGENT_PSEUDONYM_KEY", KEY)
    assert main(["import-export", str(EXAMPLE), "--output", str(output), "--dry-run"]) == 0
    assert not output.exists()
    assert main(["import-export", str(EXAMPLE), "--output", str(output)]) == 0
    assert "imported: 3 incident(s)" in capsys.readouterr().out
    assert main(["import-export", str(EXAMPLE), "--output", str(output)]) == 1  # all rejected: already there


def test_cli_without_key_refuses(tmp_path, monkeypatch, capsys):
    from hpc_agent.main import main

    monkeypatch.delenv("HPC_AGENT_PSEUDONYM_KEY", raising=False)
    assert main(["import-export", str(EXAMPLE), "--output", str(tmp_path / "data")]) == 2
    assert "HPC_AGENT_PSEUDONYM_KEY" in capsys.readouterr().err
