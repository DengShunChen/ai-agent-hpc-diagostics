import pytest

from hpc_agent.safety.policy import Operation
from hpc_agent.tools.gateway import (
    FORBIDDEN_TOOL_NAMES,
    ToolArgumentError,
    ToolGateway,
    ToolNotAllowedError,
    ToolResult,
    ToolSpec,
    ToolStatus,
)

SHELL_LIKE = sorted(FORBIDDEN_TOOL_NAMES) + [
    "exec", "eval", "sudo_read", "os_system", "popen", "spawn_process", "reboot_node", "delete_ost",
]
EXPECTED_TOOLS = (
    "analyze_logs",  # fixed analyses over the saved, de-identified log index; read-only, no expressions
    "get_filesystem_usage",
    "get_gpu_health",
    "get_job_status",
    "get_log_digest",  # error lines on the failed job's nodes while it ran; fixed terms, read-only, no arguments
    "get_log_lines",  # searches an incident's saved, de-identified log index; read-only
    "get_network_status",
    "get_node_status",
    "get_ost_usage",
    "load_skill",  # Phase C: reads a repository skill (reference only); read-only
    "search_incident_history",
    "search_manual",
    "search_sop",
)


def _spec(name, operation=Operation.READ_TEST_DATA, parameters=frozenset({"incident_id"})):
    return ToolSpec(
        name=name, description="test", operation=operation, parameters=parameters,
        handler=lambda **_: ToolResult(tool=name, status=ToolStatus.OK),
    )


def test_registry_is_exactly_the_allowlist(gateway):
    assert gateway.registered_tools() == EXPECTED_TOOLS


def test_registered_tool_accepted(gateway):
    result = gateway.invoke("get_gpu_health", incident_id="POC-GPU-001")
    assert result.status is ToolStatus.OK
    assert result.source == "test_data/gpu/POC-GPU-001/gpu_health.json"


@pytest.mark.parametrize("name", ["run_shell", "execute_command", "ssh_execute"])
def test_shell_tools_rejected(gateway, name):
    with pytest.raises(ToolNotAllowedError):
        gateway.invoke(name, command="id")
    with pytest.raises(ToolNotAllowedError):
        gateway.invoke(name)


@pytest.mark.parametrize("name", ["get_everything", "delete_ost", "reboot_node", "", "GET_OST_USAGE"])
def test_unknown_tool_rejected(gateway, name):
    with pytest.raises(ToolNotAllowedError):
        gateway.invoke(name, incident_id="POC-STORAGE-001")


@pytest.mark.parametrize("name", [None, 123, ["get_ost_usage"], b"get_ost_usage"])
def test_non_string_tool_name_rejected(gateway, name):
    with pytest.raises(ToolNotAllowedError):
        gateway.invoke(name, incident_id="POC-STORAGE-001")


@pytest.mark.parametrize("name", SHELL_LIKE)
def test_shell_like_tools_cannot_be_registered(name):
    gw = ToolGateway()
    with pytest.raises(ToolNotAllowedError):
        gw.register(_spec(name))
    assert gw.registered_tools() == ()


@pytest.mark.parametrize(
    "operation",
    [Operation.RUN_ARBITRARY_COMMAND, Operation.SSH_PRODUCTION, Operation.MODIFY_PRODUCTION,
     Operation.DELETE_PRODUCTION_DATA, Operation.AUTONOMOUS_REMEDIATION],
)
def test_tools_for_denied_operations_cannot_be_registered(operation):
    with pytest.raises(ToolNotAllowedError):
        ToolGateway().register(_spec("harmless_name", operation=operation))


@pytest.mark.parametrize("argument", ["path", "command", "host", "args"])
def test_tool_with_undeclared_argument_type_cannot_be_registered(argument):
    with pytest.raises(ToolNotAllowedError):
        ToolGateway().register(_spec("get_thing", parameters=frozenset({argument})))


@pytest.mark.parametrize(
    "query",
    ["ost; rm -rf /", "ost | cat", "$(reboot)", "`id`", "ost && true", "ost\nreboot", "../../etc/passwd",
     "ost > out", "'quoted'", "x" * 201, "", " leading space", "-rf"],
)
def test_query_argument_rejects_unsafe_text(gateway, query):
    with pytest.raises(ToolArgumentError):
        gateway.invoke("search_sop", query=query)


def test_query_argument_accepts_search_terms(gateway):
    assert gateway.invoke("search_sop", query="ost capacity write-failure lustre/fefs v2.1").status in ("ok", "no_data")


def test_duplicate_registration_rejected():
    gw = ToolGateway()
    gw.register(_spec("get_thing"))
    with pytest.raises(ToolNotAllowedError):
        gw.register(_spec("get_thing"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"incident_id": "POC-GPU-001", "command": "rm -rf /"},
        {"incident_id": "../../etc"},
        {"incident_id": "POC-GPU-001; reboot"},
        {"incident_id": 1},
        {"path": "/etc/shadow"},
    ],
)
def test_invalid_arguments_rejected(gateway, kwargs):
    with pytest.raises(ToolArgumentError):
        gateway.invoke("get_gpu_health", **kwargs)
