"""Operation-level safety policy. Anything not explicitly allowed is denied."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class Operation(StrEnum):
    READ_TEST_DATA = "READ_TEST_DATA"
    READ_KNOWLEDGE = "READ_KNOWLEDGE"
    CREATE_DIAGNOSIS = "CREATE_DIAGNOSIS"
    RECOMMEND_ACTION = "RECOMMEND_ACTION"
    RECORD_HUMAN_DECISION = "RECORD_HUMAN_DECISION"
    RUN_ARBITRARY_COMMAND = "RUN_ARBITRARY_COMMAND"
    SSH_PRODUCTION = "SSH_PRODUCTION"
    MODIFY_PRODUCTION = "MODIFY_PRODUCTION"
    DELETE_PRODUCTION_DATA = "DELETE_PRODUCTION_DATA"
    AUTONOMOUS_REMEDIATION = "AUTONOMOUS_REMEDIATION"


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


POC_RULES = MappingProxyType(
    {
        Operation.READ_TEST_DATA: Decision.ALLOW,
        Operation.READ_KNOWLEDGE: Decision.ALLOW,
        Operation.CREATE_DIAGNOSIS: Decision.ALLOW,
        Operation.RECOMMEND_ACTION: Decision.ALLOW,
        Operation.RECORD_HUMAN_DECISION: Decision.ALLOW,
        Operation.RUN_ARBITRARY_COMMAND: Decision.DENY,
        Operation.SSH_PRODUCTION: Decision.DENY,
        Operation.MODIFY_PRODUCTION: Decision.DENY,
        Operation.DELETE_PRODUCTION_DATA: Decision.DENY,
        Operation.AUTONOMOUS_REMEDIATION: Decision.DENY,
    }
)


class PolicyViolation(PermissionError):
    pass


class SafetyPolicy:
    def __init__(self) -> None:
        self._rules = POC_RULES

    def decide(self, operation: object) -> Decision:
        try:
            op = Operation(operation)
        except (ValueError, TypeError):
            return Decision.DENY
        return self._rules.get(op, Decision.DENY)

    def is_allowed(self, operation: object) -> bool:
        return self.decide(operation) is Decision.ALLOW

    def require(self, operation: object) -> None:
        if not self.is_allowed(operation):
            raise PolicyViolation(f"operation denied by safety policy: {operation!r}")
