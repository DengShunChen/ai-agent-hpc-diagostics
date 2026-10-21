import pytest

from hpc_agent.data_adapter import DataAccessError, ReadOnlyDataAdapter
from hpc_agent.safety.secrets import SensitiveDataError
from hpc_agent.tools.gateway import ToolGateway
from hpc_agent.tools.knowledge_tools import KeywordKnowledgeBase, KnowledgeHit, terms, tool_specs


@pytest.fixture
def kb(adapter):
    return KeywordKnowledgeBase(adapter)


def write_doc(root, collection, name, text):
    path = root / "knowledge" / collection / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return ReadOnlyDataAdapter(root)


def test_terms_drop_stopwords_numbers_and_identifiers():
    assert terms("The job on sim-gpu-node-0007 hit XID 13 with SIM-JOB-20001") == ["hit", "xid"]


@pytest.mark.parametrize(
    "collection,query,expected",
    [
        ("sop", "storage write failure ost capacity", "SOP-STOR-001"),
        ("sop", "gpu xid cuda illegal memory access", "SOP-GPU-001"),
        ("incidents", "sigkill exit code 137 oom memory", "SIM-HIST-0002"),
        ("manuals", "ost imbalance striping", "MAN-STOR-OST-BALANCE"),
    ],
)
def test_best_match(kb, collection, query, expected):
    assert kb.search(collection, query)[0].doc_id == expected


def test_weak_matches_are_dropped(kb):
    assert kb.search("sop", "banana") == []
    assert all(h.doc_id != "SOP-SCHED-001" for h in kb.search("sop", "storage capacity ost imbalance write failure"))


def test_hit_fields(kb):
    [hit, *_] = kb.search("sop", "lustre write failure capacity")
    assert hit.source == "knowledge/sop/SOP-STOR-001.md"
    assert hit.title == "Investigating job write failures on FEFS/Lustre"
    assert not hit.excerpt.lower().startswith("tags:") and len(hit.excerpt) <= 200


def test_excerpt_strips_control_characters(tmp_path):
    adapter = write_doc(tmp_path, "sop", "SOP-X.md", "# Disk check\nDisk quota\x1b[31m check \x07 step\n")
    [hit] = KeywordKnowledgeBase(adapter).search("sop", "disk quota check")
    assert "\x1b" not in hit.excerpt and "\x07" not in hit.excerpt


def test_tool_reports_no_data_without_hits(gateway):
    result = gateway.invoke("search_manual", query="banana smoothie")
    assert result.status == "no_data" and result.data is None and result.source is None


def test_knowledge_backend_is_replaceable(adapter):
    class FakeKnowledgeBase:
        def search(self, collection, query, limit=3):
            return [KnowledgeHit(doc_id="X", title="t", source=f"knowledge/{collection}/X.md", excerpt="e", score=9)]

    gw = ToolGateway()
    for spec in tool_specs(adapter, FakeKnowledgeBase()):
        gw.register(spec)
    assert gw.invoke("search_sop", query="anything").data[0]["doc_id"] == "X"


@pytest.mark.parametrize("segments", [("fixtures", "README.md"), ("knowledge", "sop", "x.json"),
                                      ("knowledge", "..", "README.md")])
def test_read_text_restricted_to_knowledge_markdown(adapter, segments):
    with pytest.raises((DataAccessError, FileNotFoundError)):
        adapter.read_text(*segments)


def test_knowledge_with_credentials_rejected(tmp_path):
    adapter = write_doc(tmp_path, "sop", "SOP-BAD.md", "# Login\npassword: hunter2-not-real\n")
    with pytest.raises(SensitiveDataError) as excinfo:
        KeywordKnowledgeBase(adapter).search("sop", "login password")
    assert "hunter2" not in str(excinfo.value)
