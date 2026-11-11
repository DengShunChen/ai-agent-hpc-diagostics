"""Simulated scenarios: reproducible, synthetic-only, every fault leaves the evidence it claims to."""

import json
import re

import pytest

from hpc_agent.ingest.deidentify import Pseudonymizer, find_identifiers
from hpc_agent.ingest.importer import import_export
from hpc_agent.ingest.raw_export import RawExportAdapter, _exit_code, expand_hostlist
from hpc_agent.runtime.definitions import load_definitions
from hpc_agent.safety.secrets import find_credential_like
from hpc_agent.simulate.catalog import hostlist
from hpc_agent.simulate.render import generate
from hpc_agent.simulate.scenario import DISTRACTORS, FAULTS, Scenario, load_scenarios, random_scenarios
from tests.conftest import PROJECT_ROOT

ALL = list(DISTRACTORS)


PLATFORMS = ("slurm-x86", "tcs-fx1000")


def scenario(fault, i=1, distractors=ALL, platform="slurm-x86"):
    return Scenario(scenario_id=f"SIM-T-{i:03d}", seed=500 + i, fault=fault, distractors=distractors, platform=platform)


def files(root):
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def test_same_seed_same_bytes(tmp_path):
    generate(scenario("ib_flap_lustre"), tmp_path / "a")
    generate(scenario("ib_flap_lustre"), tmp_path / "b")
    assert files(tmp_path / "a") == files(tmp_path / "b")


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("fault", FAULTS)
def test_every_fault_leaves_its_key_lines(tmp_path, fault, platform):
    labels = generate(scenario(fault, platform=platform), tmp_path)
    base = tmp_path / "export" / "SIM-T-001"
    if fault == "logs_missing":
        assert labels.key_lines == [] and not (base / "commands" / "sacct.txt").exists()
        assert not (base / "commands" / "pjstat.txt").exists()
        return
    assert labels.key_lines
    for key in labels.key_lines:
        source, node, line = key.split(":")
        path = {"syslog": base / "logs" / "syslog", "slurmctld": base / "logs" / "slurmctld.log",
                "pjm": base / "logs" / "pjm.log"}.get(
            source, base / "logs" / "slurmd" / f"{node}.log")
        text = path.read_text().splitlines()[int(line) - 1]
        assert node in text or source != "syslog"


def test_labels_use_only_known_categories(tmp_path):
    known = {c for d in load_definitions(PROJECT_ROOT).values() if d.llm for c in d.llm.categories}
    for i, fault in enumerate(FAULTS):
        labels = generate(scenario(fault, i), tmp_path)
        assert set(labels.leading_categories) <= known and set(labels.forbidden_categories) <= known
        assert not set(labels.leading_categories) & set(labels.forbidden_categories)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_exports_are_synthetic_only(tmp_path, platform):
    for i, fault in enumerate(FAULTS):
        generate(scenario(fault, i, platform=platform), tmp_path)
    for name, text in files(tmp_path / "export").items():
        lines = text.splitlines()
        if not name.endswith(".json"):
            assert lines[0].startswith("# SYNTHETIC"), name
        assert find_identifiers(lines) == [], name  # no IPs, e-mail, domain names or home paths
        assert not any(find_credential_like(line) for line in lines), name
        for host in re.findall(r"\+00:00 (\S+) ", text):
            assert host.startswith("sim-"), name


def test_blind_set_is_reproducible_and_bounded():
    a, b = random_scenarios(7, 20), random_scenarios(7, 20)
    assert a == b and len(a) == 20 and len({s.scenario_id for s in a}) == 20
    assert random_scenarios(8, 20) != a


@pytest.mark.parametrize("folder, platform", [("dev", "slurm-x86"), ("fx1000", "tcs-fx1000")])
def test_dev_scenarios_load(folder, platform):
    dev = load_scenarios(PROJECT_ROOT / "evaluation" / "scenarios" / folder)
    assert len(dev) >= 12 and {s.fault for s in dev} == set(FAULTS) and {s.platform for s in dev} == {platform}


def test_hostlist_round_trip():
    nodes = ["sim-node-0003", "sim-node-0004", "sim-node-0005", "sim-node-0009"]
    assert hostlist(nodes) == "sim-node-[0003-0005,0009]"
    assert expand_hostlist(hostlist(nodes)) == nodes
    assert expand_hostlist("sim-a-01,sim-b-02") == ["sim-a-01", "sim-b-02"]


@pytest.mark.parametrize("text, code", [("0:0", 0), ("1:0", 1), ("139:0", 139), ("0:9", 137), ("0:15", 143),
                                        ("0:125", None), ("x:1", None), ("", None)])
def test_sacct_exit_codes(text, code):
    assert _exit_code(text) == code


def test_raw_adapter_parses_and_importer_pseudonymizes(tmp_path):
    generate(scenario("gpu_xid48"), tmp_path / "sim")
    bundle = RawExportAdapter(tmp_path / "sim" / "export").load("SIM-T-001")
    keys = set(bundle.datasets)
    assert {("logs", "syslog"), ("logs", "slurmctld"), ("logs", "slurmd"), ("scheduler", "job_status"),
            ("scheduler", "node_status"), ("storage", "ost_usage"), ("gpu", "gpu_health")} <= keys
    gpu = bundle.datasets[("gpu", "gpu_health")]
    assert any(e["xid"] == 48 for g in gpu["gpus"] for e in g["xid_events"])
    report = import_export(RawExportAdapter(tmp_path / "sim" / "export"), tmp_path / "root", Pseudonymizer("k" * 40))
    assert report.imported == ["SIM-T-001"] and not report.rejected
    imported = (tmp_path / "root" / "test_data" / "logs" / "SIM-T-001" / "syslog.json").read_text()
    assert "sim-gpu-node-" not in imported and "sim-node-" not in imported  # every logging host pseudonymized
    job = json.loads((tmp_path / "root" / "test_data" / "scheduler" / "SIM-T-001" / "job_status.json").read_text())
    assert job["job_id"].startswith("job-") and job["job_id"] not in ("",)


def test_raw_adapter_counts_unparsable_lines_and_refuses_symlinks(tmp_path):
    generate(scenario("segfault", distractors=[]), tmp_path / "sim")
    base = tmp_path / "sim" / "export" / "SIM-T-001"
    with (base / "logs" / "syslog").open("a") as handle:
        handle.write("garbage without a timestamp\n")
    assert RawExportAdapter(tmp_path / "sim" / "export").load("SIM-T-001").datasets[("logs", "syslog")]["unparsed"] == 1
    (base / "logs" / "slurmctld.log").unlink()
    (base / "logs" / "slurmctld.log").symlink_to(base / "logs" / "syslog")
    with pytest.raises(ValueError, match="not a regular file"):
        RawExportAdapter(tmp_path / "sim" / "export").load("SIM-T-001")


def test_simulate_cli_refuses_output_inside_the_repository(capsys):
    from hpc_agent.main import main

    assert main(["simulate", "--blind-seed", "1", "--count", "1", "--output", str(PROJECT_ROOT / "x")]) == 2
    assert "outside the repository" in capsys.readouterr().err


def test_blind_cli_hides_fault_names(tmp_path, capsys):
    from hpc_agent.main import main

    assert main(["simulate", "--blind-seed", "3", "--count", "2", "--output", str(tmp_path / "blind")]) == 0
    out = capsys.readouterr().out
    assert not any(f", fault {name}" in out for name in FAULTS) and "SIM-BLIND-001" in out


def test_tcs_export_parses_pjm_tofu_and_node_state(tmp_path):
    generate(scenario("ib_flap_lustre", platform="tcs-fx1000"), tmp_path / "sim")
    base = tmp_path / "sim" / "export" / "SIM-T-001"
    assert (base / "logs" / "pjm.log").is_file() and not (base / "logs" / "slurmd").exists()
    bundle = RawExportAdapter(tmp_path / "sim" / "export").load("SIM-T-001")
    job = bundle.datasets[("scheduler", "job_status")]
    assert job["scheduler"] == "pjm" and job["exit_code"] == 5 and job["allocated_nodes"][0].startswith("sim-fx-")
    links = bundle.datasets[("network", "network_status")]["interfaces"]
    assert any(i["link_flaps"] >= 4 and i["interface"] == "tofu/TNI2-C" for i in links)
    assert ("logs", "pjm") in bundle.datasets
    report = import_export(RawExportAdapter(tmp_path / "sim" / "export"), tmp_path / "root", Pseudonymizer("k" * 40))
    assert report.imported == ["SIM-T-001"] and not report.rejected


def test_tcs_walltime_reason_reaches_the_job_record(tmp_path):
    generate(scenario("walltime", platform="tcs-fx1000", distractors=[]), tmp_path / "sim")
    job = RawExportAdapter(tmp_path / "sim" / "export").load("SIM-T-001").datasets[("scheduler", "job_status")]
    assert job["termination_reason"] == "ELAPSE LIMIT EXCEEDED" and job["elapsed"] == job["requested_walltime"]


@pytest.mark.parametrize("platform, source", [("slurm-x86", "slurmctld"), ("tcs-fx1000", "pjm")])
def test_job_manager_log_lines_carry_no_node(tmp_path, platform, source):
    """The daemon name must not be pseudonymized like a host: it looked like the job's compute node."""
    generate(scenario("gpu_xid79", platform=platform), tmp_path / "sim")
    import_export(RawExportAdapter(tmp_path / "sim" / "export"), tmp_path / "root", Pseudonymizer("k" * 40))
    index = json.loads((tmp_path / "root" / "test_data" / "logs" / "SIM-T-001" / f"{source}.json").read_text())
    assert index["lines"] and all("node" not in line for line in index["lines"])
    nodes = {line["node"] for line in json.loads(
        (tmp_path / "root" / "test_data" / "logs" / "SIM-T-001" / "syslog.json").read_text())["lines"]}
    job = json.loads((tmp_path / "root" / "test_data" / "scheduler" / "SIM-T-001" / "job_status.json").read_text())
    assert set(job["allocated_nodes"]) <= nodes  # job nodes keep matching pseudonyms across datasets


def test_cited_job_manager_key_line_is_counted():
    from types import SimpleNamespace

    from hpc_agent.evaluation.scenarios import SIMULATION_KEY, _cited

    evidence = SimpleNamespace(statement='line=42: text="JOB 7 KILLED: memory limit exceeded"',
                               source="test_data/logs/SIM-T-001/pjm.json")
    run = SimpleNamespace(diagnoses=[SimpleNamespace(evidence=[evidence])],
                          rca=SimpleNamespace(facts=[], probable_causes=[]))
    assert _cited(run, Pseudonymizer(SIMULATION_KEY)) == {("pjm", None, 42)}
