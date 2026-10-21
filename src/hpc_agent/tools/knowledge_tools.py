"""Knowledge tools: search_incident_history, search_sop, search_manual.

Read-only keyword search over knowledge/*.md behind the KnowledgeBase interface, so a
RAG backend can replace KeywordKnowledgeBase later without changing callers. Document
text is untrusted reference material: it is returned as data and never interpreted as
instructions or executed.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.safety.policy import Operation
from hpc_agent.tools.gateway import ToolResult, ToolSpec, ToolStatus

COLLECTIONS = {
    "search_incident_history": "incidents",
    "search_sop": "sop",
    "search_manual": "manuals",
}
MIN_MATCHING_TERMS = 2
MAX_HITS = 3
EXCERPT_CHARS = 200

_TOKEN = re.compile(r"[a-z0-9]+")
# Identifiers such as node names or job IDs ("sim-gpu-node-0007", "SIM-JOB-20001") say
# nothing about the topic and would match unrelated documents.
_IDENTIFIER = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)*-\d+\b")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
STOPWORDS = frozenset(
    "the and for with that this from may have has are was were not but its into than rather "
    "while even when which what been can could would should does did also only alone itself "
    "possible possibly cause causes include includes including contributing contribute "
    "commonly associated indicated needed before any data saved shows show none "
    "sim job jobs".split()
)


def terms(text: str) -> list[str]:
    """Distinct lowercase search terms, in order of appearance."""
    text = _IDENTIFIER.sub(" ", text.lower())
    words = [w for w in _TOKEN.findall(text) if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()]
    return list(dict.fromkeys(words))


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    title: str
    source: str  # e.g. "knowledge/sop/SOP-STOR-001.md"
    excerpt: str
    score: int


class KnowledgeBase(Protocol):
    def search(self, collection: str, query: str, limit: int = MAX_HITS) -> list[KnowledgeHit]: ...


class _Document(BaseModel):
    doc_id: str
    title: str
    source: str
    body: list[str]
    title_terms: frozenset[str]
    all_terms: frozenset[str]


class KeywordKnowledgeBase:
    """Scores documents by how many distinct query terms they contain (title terms count double)."""

    def __init__(self, adapter: ReadOnlyDataAdapter) -> None:
        self._adapter = adapter
        self._cache: dict[str, list[_Document]] = {}

    def _documents(self, collection: str) -> list[_Document]:
        if collection not in self._cache:
            docs = []
            for name in self._adapter.list_files("knowledge", collection, suffix=".md"):
                lines = self._adapter.read_text("knowledge", collection, name).splitlines()
                title = lines[0].removeprefix("# ").strip() if lines and lines[0].startswith("# ") else name
                body = [line.strip() for line in lines[1:] if line.strip()]
                docs.append(
                    _Document(
                        doc_id=name.removesuffix(".md"),
                        title=title,
                        source=f"knowledge/{collection}/{name}",
                        body=body,
                        title_terms=frozenset(terms(title)),
                        all_terms=frozenset(terms(" ".join([title, *body]))),
                    )
                )
            self._cache[collection] = docs
        return self._cache[collection]

    def search(self, collection: str, query: str, limit: int = MAX_HITS) -> list[KnowledgeHit]:
        wanted = set(terms(query))
        hits = []
        for doc in self._documents(collection):
            matched = wanted & doc.all_terms
            if len(matched) < MIN_MATCHING_TERMS:
                continue
            score = len(matched) + len(wanted & doc.title_terms)
            excerpt = next(
                (line for line in doc.body if not line.lower().startswith("tags:") and set(terms(line)) & matched),
                doc.title,
            )
            hits.append(
                KnowledgeHit(
                    doc_id=doc.doc_id,
                    title=doc.title,
                    source=doc.source,
                    excerpt=_CONTROL.sub(" ", excerpt)[:EXCERPT_CHARS],
                    score=score,
                )
            )
        if not hits:
            return []
        # Keep only hits reasonably close to the best match to avoid generic-word noise.
        best = max(h.score for h in hits)
        hits = [h for h in hits if h.score * 2 >= best]
        return sorted(hits, key=lambda h: (-h.score, h.doc_id))[:limit]


def tool_specs(adapter: ReadOnlyDataAdapter, knowledge: KnowledgeBase | None = None) -> list[ToolSpec]:
    kb = knowledge or KeywordKnowledgeBase(adapter)

    def spec(name: str, collection: str) -> ToolSpec:
        def handler(query: str) -> ToolResult:
            hits = kb.search(collection, query)
            return ToolResult(
                tool=name,
                status=ToolStatus.OK if hits else ToolStatus.NO_DATA,
                source=f"knowledge/{collection}" if hits else None,
                data=[h.model_dump() for h in hits] or None,
            )

        return ToolSpec(
            name=name,
            description=f"Keyword search over knowledge/{collection} (reference material only)",
            operation=Operation.READ_KNOWLEDGE,
            parameters=frozenset({"query"}),
            handler=handler,
        )

    return [spec(name, collection) for name, collection in COLLECTIONS.items()]
