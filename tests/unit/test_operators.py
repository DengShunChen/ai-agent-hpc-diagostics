import json
import os

import pytest

from hpc_agent.safety.operators import OperatorAuthError, OperatorTokens, hash_token, issue_token


def test_issue_and_authenticate(tmp_path):
    path = tmp_path / "operators.json"
    token = issue_token(path, "ops.wang")
    assert len(token) >= 40
    assert token not in path.read_text()  # only the hash is stored
    assert json.loads(path.read_text())["operators"]["ops.wang"] == hash_token(token)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert OperatorTokens.from_file(path).authenticate(token) == "ops.wang"


def test_rotation_invalidates_old_token(tmp_path):
    path = tmp_path / "operators.json"
    old = issue_token(path, "ops.wang")
    new = issue_token(path, "ops.wang")
    tokens = OperatorTokens.from_file(path)
    assert tokens.authenticate(new) == "ops.wang"
    with pytest.raises(OperatorAuthError):
        tokens.authenticate(old)


@pytest.mark.parametrize("token", [None, "", "wrong", "sha256:" + "0" * 64])
def test_bad_tokens_rejected(tmp_path, token):
    path = tmp_path / "operators.json"
    issue_token(path, "ops.wang")
    with pytest.raises(OperatorAuthError):
        OperatorTokens.from_file(path).authenticate(token)


@pytest.mark.parametrize("operator", ["storage-agent", "rca-coordinator", "system", "claude", "x"])
def test_ai_identities_cannot_hold_tokens(tmp_path, operator):
    with pytest.raises(OperatorAuthError):
        issue_token(tmp_path / "operators.json", operator)
    with pytest.raises(OperatorAuthError):
        OperatorTokens({operator: hash_token("t")})


def test_world_readable_file_refused(tmp_path):
    path = tmp_path / "operators.json"
    issue_token(path, "ops.wang")
    os.chmod(path, 0o644)
    with pytest.raises(OperatorAuthError, match="chmod 600"):
        OperatorTokens.from_file(path)


def test_token_file_inside_repository_refused(tmp_path):
    with pytest.raises(OperatorAuthError, match="outside the repository"):
        issue_token(tmp_path / "repo" / "operators.json", "ops.wang", forbidden_root=tmp_path / "repo")


@pytest.mark.parametrize("content", ['{"operators": {"ops.wang": "plaintext"}}', "[]", '{"operators": []}'])
def test_malformed_file_refused(tmp_path, content):
    path = tmp_path / "operators.json"
    path.write_text(content)
    os.chmod(path, 0o600)
    with pytest.raises(OperatorAuthError):
        OperatorTokens.from_file(path)
