from hpc_agent.agents import RCACoordinator, TriageAgent
from hpc_agent.models import AgentDiagnosis, Evidence, EvidenceKind, Incident, ProbableCause

INCIDENT = Incident(incident_id="INC-001", severity="high", node="sim-node-0001",
                    symptoms=["job aborted with CUDA error"])


def ev(source, statement, kind=EvidenceKind.TOOL_RESULT):
    return Evidence(kind=kind, source=source, statement=statement)


REPORTED = ev("INC-001:symptoms[0]", "job aborted with CUDA error", EvidenceKind.INCIDENT_REPORT)
GPU_EV = ev("tool:get_gpu_health:x", "GPU 0 XID 79 count=1")
NODE_EV = ev("tool:get_node_status:y", "node sim-node-0001 state=DRAIN")


def diag(agent, domain, evidence, causes=(), components=(), missing=(), checks=()):
    return AgentDiagnosis(agent=agent, domain=domain, evidence=evidence, possible_causes=list(causes),
                          affected_components=list(components), missing_information=list(missing),
                          recommended_checks=list(checks),
                          confidence=max((c.confidence for c in causes), default=0.0))


def cause(text, domain, support, confidence, **kw):
    return ProbableCause(cause=text, domain=domain, supporting_evidence=support, confidence=confidence, **kw)


def correlate(diagnoses, unavailable=()):
    return RCACoordinator().correlate(INCIDENT, TriageAgent().triage(INCIDENT), diagnoses, unavailable)


GPU_DIAG = diag("gpu-agent", "gpu", [REPORTED, GPU_EV], [cause("gpu hw", "gpu", [GPU_EV], 0.5)],
                ["node:sim-node-0001", "gpu:sim-node-0001/0"])
SCHED_DIAG = diag("scheduler-agent", "scheduler", [REPORTED, NODE_EV], [cause("node drain", "scheduler", [NODE_EV], 0.3)],
                  ["node:sim-node-0001"])


def test_ranks_by_confidence_and_never_confirms():
    rca = correlate([SCHED_DIAG, GPU_DIAG])
    assert [c.cause for c in rca.probable_causes] == ["gpu hw", "node drain"]
    assert rca.confirmed_root_cause is None
    assert rca.confidence == 0.5
    assert "Root cause NOT confirmed" in rca.summary


def test_tool_backed_hypotheses_in_two_domains_is_multi_domain():
    rca = correlate([GPU_DIAG, SCHED_DIAG])
    assert rca.classification == "multi-domain"
    assert rca.affected_domains == ["gpu", "scheduler"]


def test_cross_domain_relationships():
    rca = correlate([GPU_DIAG, SCHED_DIAG])
    assert any("job aborted with CUDA error" in r and "gpu and scheduler" in r for r in rca.cross_domain_relationships)
    assert "node:sim-node-0001 is implicated by gpu and scheduler findings" in rca.cross_domain_relationships


def test_unexamined_domains_become_checks():
    rca = correlate([SCHED_DIAG])
    assert "get_network_status(): network domain not examined in this run (node sim-node-0001 implicated)" in rca.recommended_checks
    assert any(c.startswith("get_gpu_health()") for c in rca.recommended_checks)


def test_ruled_out_kept_with_conflicts():
    ruled = cause("walltime", "scheduler", [], 0.0, status="ruled_out", conflicting_evidence=[NODE_EV])
    rca = correlate([diag("scheduler-agent", "scheduler", [NODE_EV], [ruled])])
    assert rca.probable_causes == [ruled]
    assert rca.conflicting_evidence == [NODE_EV]
    assert rca.confidence == 0.0 and rca.affected_domains == []
    assert "No hypothesis is supported" in rca.summary


def test_tie_reported_in_summary():
    a = diag("gpu-agent", "gpu", [GPU_EV], [cause("a", "gpu", [GPU_EV], 0.3)])
    b = diag("scheduler-agent", "scheduler", [NODE_EV], [cause("b", "scheduler", [NODE_EV], 0.3)])
    assert "2 hypotheses are tied at confidence 0.30" in correlate([a, b]).summary


def test_unavailable_agents_reported_missing():
    rca = correlate([], unavailable=["gpu-agent"])
    assert "gpu-agent not available in this iteration: its domain was not examined" in rca.missing_information


def test_facts_deduplicated_and_traceable():
    rca = correlate([GPU_DIAG, SCHED_DIAG])
    keys = [(e.source, e.statement) for e in rca.facts]
    assert len(keys) == len(set(keys))
    assert set(keys) == {(e.source, e.statement) for e in [REPORTED, GPU_EV, NODE_EV]}
