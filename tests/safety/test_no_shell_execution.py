"""Static guard: application code must contain no process-execution or remote-access paths."""

import re

import pytest

from tests.conftest import PROJECT_ROOT

FORBIDDEN = {
    "subprocess import": r"^\s*(import|from)\s+subprocess\b",
    "os process exec": r"\bos\.(system|popen|exec\w*|spawn\w*|fork)\s*\(",
    "shell=True": r"\bshell\s*=\s*True\b",
    "eval/exec": r"(?<![\w.])(eval|exec)\s*\(",
    "remote/pty libs": r"^\s*(import|from)\s+(paramiko|fabric|pexpect|pty|asyncssh|telnetlib)\b",
    "asyncio subprocess": r"\bcreate_subprocess_(exec|shell)\b",
    "dynamic import": r"\b__import__\s*\(|\bimportlib\.import_module\s*\(",
    "framework script runner": r"\bSkillScriptRunner\b",
    "unsafe yaml load": r"\byaml\.(load|unsafe_load|full_load)\s*\(",
}
SOURCES = sorted((PROJECT_ROOT / "src").rglob("*.py"))


def test_sources_found():
    assert SOURCES


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_no_execution_primitives(path):
    text = path.read_text(encoding="utf-8")
    hits = [name for name, pattern in FORBIDDEN.items() if re.search(pattern, text, re.MULTILINE)]
    assert hits == [], f"{path.name}: {hits}"


def test_no_forbidden_tool_functions_defined():
    pattern = re.compile(
        r"^\s*def\s+(run_shell|execute_command|ssh_execute|run_script|run_linux_command)\b",
        re.MULTILINE,
    )
    assert not any(pattern.search(p.read_text(encoding="utf-8")) for p in SOURCES)


def test_shell_scripts_are_syntactically_valid_and_never_use_sudo():
    import subprocess  # test-only: checks the repository's own helper scripts

    for script in sorted((PROJECT_ROOT / "scripts").glob("*.sh")):
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0, script.name
        text = script.read_text()
        assert not re.search(r"\bsudo\b|\bdnf\b|\bsystemctl\b|firewall-cmd|setenforce|--privileged|--network[= ]host",
                             text), script.name
