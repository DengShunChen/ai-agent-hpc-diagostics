import json
import stat
import threading

import pytest

from hpc_agent.logging.audit_logger import GENESIS, AppendOnlyLog, AuditIntegrityError


@pytest.fixture
def log(tmp_path):
    return AppendOnlyLog(tmp_path / "audit" / "log.jsonl")


def lines(log):
    return log.path.read_text().splitlines()


def test_append_and_read(log):
    h0 = log.append({"a": 1})
    h1 = log.append({"b": "two"})
    assert log.records() == [{"a": 1}, {"b": "two"}]
    first, second = map(json.loads, lines(log))
    assert first["prev_hash"] == GENESIS and first["hash"] == h0 and first["seq"] == 0
    assert second["prev_hash"] == h0 and second["hash"] == h1 and second["seq"] == 1


def test_empty_log(log):
    assert log.records() == [] and log.verify() == 0


def test_file_is_private(log):
    log.append({"a": 1})
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def _rewrite(log, new_lines):
    log.path.write_text("\n".join(new_lines) + "\n")


def test_modified_record_detected(log):
    for i in range(3):
        log.append({"i": i})
    entries = [json.loads(x) for x in lines(log)]
    entries[1]["record"]["i"] = 99
    _rewrite(log, [json.dumps(e) for e in entries])
    with pytest.raises(AuditIntegrityError, match="line 2"):
        log.verify()


def test_deleted_record_detected(log):
    for i in range(3):
        log.append({"i": i})
    _rewrite(log, [lines(log)[0], lines(log)[2]])
    with pytest.raises(AuditIntegrityError, match="line 2"):
        log.verify()


def test_reordered_records_detected(log):
    for i in range(3):
        log.append({"i": i})
    a, b, c = lines(log)
    _rewrite(log, [a, c, b])
    with pytest.raises(AuditIntegrityError):
        log.verify()


def test_garbage_line_detected(log):
    log.append({"i": 0})
    _rewrite(log, [*lines(log), "not json"])
    with pytest.raises(AuditIntegrityError, match="line 2"):
        log.verify()


def test_concurrent_appends_keep_chain(log):
    threads = [threading.Thread(target=lambda n=n: log.append({"n": n})) for n in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["n"] for r in log.records()) == list(range(40))
