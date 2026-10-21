import pytest

from hpc_agent.config import ConfigError, load_agent_tool_scopes


def test_repo_scopes(scopes):
    assert scopes["triage-agent"] == frozenset()
    assert scopes["storage-agent"] == {"get_filesystem_usage", "get_ost_usage"}
    assert scopes["scheduler-agent"] == {"get_job_status", "get_node_status"}


@pytest.mark.parametrize(
    "text",
    ["[]", "agents: []", "agents:\n  storage-agent:\n    tools: get_ost_usage",
     "agents:\n  storage-agent:\n    tools: [1, 2]", "agents:\n  storage-agent: oops"],
)
def test_malformed_config_fails_closed(tmp_path, text):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(text)
    with pytest.raises(ConfigError):
        load_agent_tool_scopes(tmp_path)


def test_yaml_tags_not_executed(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text("agents: !!python/object/apply:os.getcwd []")
    with pytest.raises(Exception):
        load_agent_tool_scopes(tmp_path)
