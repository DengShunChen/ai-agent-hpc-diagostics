import pytest

from hpc_agent.config import ConfigError, load_agent_tool_scopes


def test_repo_scopes(scopes):
    assert scopes["triage-agent"] == frozenset()
    knowledge = {"search_sop", "search_incident_history", "search_manual", "load_skill"}
    knowledge |= {"get_log_lines", "analyze_logs"}  # every specialist may search and analyse saved logs (read-only)
    assert scopes["storage-agent"] == {"get_filesystem_usage", "get_ost_usage"} | knowledge
    assert scopes["scheduler-agent"] == {"get_job_status", "get_node_status", "get_log_digest"} | knowledge
    assert scopes["network-agent"] == {"get_network_status"} | knowledge
    assert scopes["gpu-agent"] == {"get_gpu_health"} | knowledge
    assert scopes["rca-coordinator"] == frozenset()


GOOD = """---
name: storage-agent
description: "Storage"
domain: storage
tools: [get_ost_usage]
---
"""


@pytest.mark.parametrize(
    "text",
    ["no frontmatter", "---\n[]\n---\n", GOOD.replace("tools: [get_ost_usage]", "tools: get_ost_usage"),
     GOOD.replace("tools: [get_ost_usage]", "tools: [1, 2]"), GOOD.replace("name: storage-agent", "name: other-agent"),
     GOOD.replace('description: "Storage"\n', ""), GOOD.replace("domain: storage", "domain: storage\nexecute: true"),
     GOOD + "a prompt without an llm section"],
)
def test_malformed_definitions_fail_closed(tmp_path, text):
    (tmp_path / "config" / "agents").mkdir(parents=True)
    (tmp_path / "config" / "agents" / "storage-agent.md").write_text(text)
    with pytest.raises(ConfigError):
        load_agent_tool_scopes(tmp_path)


def test_missing_definitions_fail_closed(tmp_path):
    with pytest.raises(ConfigError):
        load_agent_tool_scopes(tmp_path)


def test_yaml_tags_not_executed(tmp_path):
    (tmp_path / "config" / "agents").mkdir(parents=True)
    (tmp_path / "config" / "agents" / "storage-agent.md").write_text(
        "---\nname: !!python/object/apply:os.getcwd []\n---\n")
    with pytest.raises(Exception):
        load_agent_tool_scopes(tmp_path)


def test_definition_scopes_cannot_widen_the_allowlist(tmp_path, gateway):
    from hpc_agent.tools.gateway import ScopedGateway

    (tmp_path / "config" / "agents").mkdir(parents=True)
    (tmp_path / "config" / "agents" / "storage-agent.md").write_text(GOOD.replace("[get_ost_usage]", "[get_ost_usage, run_shell]"))
    scopes = load_agent_tool_scopes(tmp_path)
    assert ScopedGateway(gateway, "storage-agent", scopes, []).allowed_tools == {"get_ost_usage"}
