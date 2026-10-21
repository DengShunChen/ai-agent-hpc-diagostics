import pytest

from hpc_agent.safety.policy import Decision, Operation, PolicyViolation, SafetyPolicy

ALLOWED = ["READ_TEST_DATA", "READ_KNOWLEDGE", "CREATE_DIAGNOSIS", "RECOMMEND_ACTION",
           "RECORD_HUMAN_DECISION", "GENERATE_EXPLANATION"]
DENIED = ["RUN_ARBITRARY_COMMAND", "SSH_PRODUCTION", "MODIFY_PRODUCTION",
          "DELETE_PRODUCTION_DATA", "AUTONOMOUS_REMEDIATION"]


@pytest.mark.parametrize("op", ALLOWED)
def test_allowed_operations(op):
    assert SafetyPolicy().decide(op) is Decision.ALLOW
    SafetyPolicy().require(Operation(op))


@pytest.mark.parametrize("op", DENIED)
def test_denied_operations(op):
    policy = SafetyPolicy()
    assert policy.decide(op) is Decision.DENY
    with pytest.raises(PolicyViolation):
        policy.require(op)


@pytest.mark.parametrize("op", ["FORMAT_DISK", "read_test_data", "", None, 0, ["READ_TEST_DATA"], object()])
def test_unknown_operations_default_deny(op):
    policy = SafetyPolicy()
    assert policy.decide(op) is Decision.DENY
    assert not policy.is_allowed(op)


def test_every_operation_has_an_explicit_rule():
    from hpc_agent.safety.policy import POC_RULES
    assert set(POC_RULES) == set(Operation)
    assert {o for o, d in POC_RULES.items() if d is Decision.ALLOW} == set(map(Operation, ALLOWED))


def test_rules_are_read_only():
    from hpc_agent.safety.policy import POC_RULES
    with pytest.raises(TypeError):
        POC_RULES[Operation.SSH_PRODUCTION] = Decision.ALLOW
