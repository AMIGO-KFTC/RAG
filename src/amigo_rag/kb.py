"""KnowledgeBase: 파싱 → 청킹 → 임베딩 → ChromaDB 저장, 그리고 하이브리드 검색을 묶은 진입점.

    kb = KnowledgeBase("session-123")
    kb.add_file("업무정의서.pdf")
    kb.add_url("https://confluence.example.com/pages/viewpage.action?pageId=123")
    kb.add_text("홈페이지 유지보수 업체 담당자는 ...", source_name="인계자 답변", source_type="chat")
    for r in kb.search("홈페이지 담당자"):
        print(r.citation, r.score, r.text[:80])
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chunking import chunk_document
from .config import RAGSettings
from .connectors import fetch_url
from .embeddings import Embedder, get_embedder, tokenize
from .models import Chunk, IngestResult, ParsedDocument, ParseError, SearchResult, SourceInfo
from .parsers import parse_file
from .parsers.text import text_to_blocks
from .retrieval import BM25Index, reciprocal_rank_fusion
from .store import ChromaStore

_VERSIONS: dict[tuple[str, str], int] = {}
_BM25_CACHE: dict[tuple[str, str], tuple[tuple[int, int], BM25Index]] = {}
_CACHE_LOCK = threading.RLock()

# 소스 목록에 노출할 메타데이터 키
_SOURCE_META_KEYS = ("url", "title", "author", "sender", "sender_name", "sent_at", "subject", "ingested_at", "space", "file_type")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KnowledgeBase:
    def __init__(self, kb_id: str = "default", *, settings: RAGSettings | None = None, embedder: Embedder | None = None) -> None:
        self.kb_id = kb_id
        self.settings = settings or RAGSettings()
        self.embedder = embedder or get_embedder(self.settings.embedding)
        self.store = ChromaStore(self.settings.chroma_dir, self.embedder.name)
        self._cache_key = (str(self.settings.chroma_dir or ":memory:"), kb_id)

    # ------------------------------------------------------------------ 적재(ingest)
    def add_file(
        self,
        path: str | Path,
        *,
        source_id: str | None = None,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        path = Path(path)
        if not path.is_file():
            raise ParseError(f"파일을 찾을 수 없습니다: {path}")
        if path.stat().st_size > self.settings.max_file_mb * 1024 * 1024:
            raise ParseError(f"파일이 너무 큽니다(최대 {self.settings.max_file_mb}MB).")
        doc = parse_file(path, display_name=display_name)
        return self.add_document(doc, source_id=source_id, metadata=metadata)

    def add_url(
        self,
        url: str,
        *,
        source_id: str | None = None,
        link_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        client=None,
    ) -> IngestResult:
        doc = fetch_url(url, self.settings, link_type=link_type, client=client)
        return self.add_document(doc, source_id=source_id, metadata={"url": url, **(metadata or {})})

    def add_text(
        self,
        text: str,
        *,
        source_name: str,
        source_type: str = "note",
        source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """대화 중 사용자가 알려준 내용처럼 파일이 아닌 텍스트를 지식베이스에 추가한다."""
        doc = ParsedDocument(
            source_name=source_name,
            source_type=source_type,
            blocks=text_to_blocks(text),
            metadata={"created_at": _now(), **(metadata or {})},
        )
        return self.add_document(doc, source_id=source_id)

    def add_document(
        self,
        doc: ParsedDocument,
        *,
        source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        source_id = source_id or uuid.uuid4().hex[:12]
        chunks = chunk_document(
            doc,
            source_id=source_id,
            chunk_size=self.settings.chunk_size,
            chunk_overlap=self.settings.chunk_overlap,
            extra_metadata={"ingested_at": _now(), **(metadata or {})},
        )
        # 같은 source_id 로 다시 올리면 기존 청크를 교체한다.
        self.store.delete(self.kb_id, {"source_id": source_id})
        warnings = list(doc.warnings)
        for att in doc.attachments:
            warnings.extend(att.warnings)
        if chunks:
            vectors = self.embedder.embed_documents([contextual_text(c) for c in chunks])
            self.store.add(
                self.kb_id,
                [c.id for c in chunks],
                [c.text for c in chunks],
                vectors,
                [c.metadata for c in chunks],
            )
        else:
            warnings.append("추출된 텍스트가 없습니다.")
        self._touch()
        doc_meta = {k: v for k, v in doc.metadata.items() if isinstance(v, (str, int, float, bool)) and v != ""}
        return IngestResult(
            source_id=source_id,
            source_name=doc.source_name,
            source_type=doc.source_type,
            chunk_count=len(chunks),
            char_count=sum(len(c.text) for c in chunks),
            metadata={**doc_meta, "attachment_count": len(doc.attachments)},
            warnings=warnings,
        )

    # ------------------------------------------------------------------ 검색
    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        source_types: list[str] | None = None,
        source_ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        per_source_limit: int | None = None,
    ) -> list[SearchResult]:
        """하이브리드(벡터 + BM25) 검색. 결과는 관련도 순이며 각 결과에 출처 메타데이터가 붙는다."""
        query = (query or "").strip()
        if not query or top_k <= 0:
            return []
        where = _combine_where(where, source_types, source_ids)
        pool = max(top_k * 4, 20)
        query_vec = self.embedder.embed_query(query)

        vector_rows = self.store.query(self.kb_id, query_vec, pool, where)
        keyword_hits = self._bm25().search(query, pool, where)
        fused = reciprocal_rank_fusion(
            [[r["id"] for r in vector_rows], [d.id for d, _ in keyword_hits]],
            weights=[1.0, 1.0],
        )
        rows: dict[str, dict] = {r["id"]: r for r in vector_rows}
        for doc, _ in keyword_hits:
            rows.setdefault(doc.id, {"id": doc.id, "text": doc.text, "metadata": dict(doc.metadata), "similarity": None})
        missing = [cid for cid, row in rows.items() if row["similarity"] is None]
        if missing:
            for cid, sim in self.store.similarities(self.kb_id, missing, query_vec).items():
                rows[cid]["similarity"] = sim

        q_tokens = set(tokenize(query))
        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        results: list[SearchResult] = []
        per_source: dict[str, int] = {}
        for cid, _fused in ranked:
            row = rows[cid]
            sid = str(row["metadata"].get("source_id", ""))
            if per_source_limit and per_source.get(sid, 0) >= per_source_limit:
                continue
            per_source[sid] = per_source.get(sid, 0) + 1
            coverage = _coverage(q_tokens, row)
            similarity = float(row["similarity"] or 0.0)
            results.append(
                SearchResult(
                    chunk_id=cid,
                    text=row["text"],
                    score=round(0.5 * max(similarity, 0.0) + 0.5 * coverage, 4),
                    metadata=row["metadata"],
                    vector_score=round(similarity, 4),
                    keyword_score=round(coverage, 4),
                )
            )
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------ 관리
    def list_sources(self) -> list[SourceInfo]:
        sources: dict[str, SourceInfo] = {}
        for row in self.store.get(self.kb_id):
            meta = row["metadata"]
            sid = str(meta.get("source_id", ""))
            if sid not in sources:
                sources[sid] = SourceInfo(
                    source_id=sid,
                    source_name=str(meta.get("source_name", "")),
                    source_type=str(meta.get("source_type", "")),
                    chunk_count=0,
                    metadata={k: meta[k] for k in _SOURCE_META_KEYS if k in meta},
                )
            sources[sid].chunk_count += 1
        return sorted(sources.values(), key=lambda s: str(s.metadata.get("ingested_at", "")))

    def get_chunks(self, source_id: str | None = None, limit: int | None = None) -> list[SearchResult]:
        """원문 청크를 순서대로 돌려준다(요약·개요 작성용). 점수는 0."""
        rows = self.store.get(self.kb_id, {"source_id": source_id} if source_id else None)
        rows.sort(key=lambda r: (str(r["metadata"].get("source_id")), int(r["metadata"].get("chunk_index", 0))))
        if limit:
            rows = rows[:limit]
        return [SearchResult(chunk_id=r["id"], text=r["text"], score=0.0, metadata=r["metadata"]) for r in rows]

    def delete_source(self, source_id: str) -> int:
        removed = self.store.delete(self.kb_id, {"source_id": source_id})
        self._touch()
        return removed

    def clear(self) -> None:
        self.store.drop(self.kb_id)
        self._touch()

    def stats(self) -> dict[str, Any]:
        return {
            "kb_id": self.kb_id,
            "chunk_count": self.store.count(self.kb_id),
            "source_count": len(self.list_sources()),
            "embedder": self.embedder.name,
        }

    # ------------------------------------------------------------------ 내부
    def _touch(self) -> None:
        with _CACHE_LOCK:
            _VERSIONS[self._cache_key] = _VERSIONS.get(self._cache_key, 0) + 1

    def _bm25(self) -> BM25Index:
        with _CACHE_LOCK:
            token = (_VERSIONS.get(self._cache_key, 0), self.store.count(self.kb_id))
            cached = _BM25_CACHE.get(self._cache_key)
            if cached and cached[0] == token:
                return cached[1]
            index = BM25Index(self.store.get(self.kb_id))
            _BM25_CACHE[self._cache_key] = (token, index)
            return index


def contextual_text(chunk: Chunk) -> str:
    """임베딩할 때는 문서 제목·섹션을 앞에 붙여 청크만 봐서는 모르는 문맥을 보강한다."""
    meta = chunk.metadata
    header = " / ".join(
        str(x) for x in (meta.get("title") or meta.get("file_name"), meta.get("section"), meta.get("subject")) if x
    )
    return f"{header}\n{chunk.text}" if header else chunk.text


def _coverage(q_tokens: set[str], row: dict) -> float:
    if not q_tokens:
        return 0.0
    meta = row.get("metadata") or {}
    doc_tokens = set(tokenize(" ".join([row.get("text") or "", str(meta.get("section", "")), str(meta.get("file_name", ""))])))
    return len(q_tokens & doc_tokens) / len(q_tokens)


def _combine_where(where, source_types, source_ids) -> dict | None:
    clauses = []
    if where:
        clauses.append(where)
    if source_types:
        clauses.append({"source_type": {"$in": list(source_types)}})
    if source_ids:
        clauses.append({"source_id": {"$in": list(source_ids)}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


# ---------------------------------------------------------------------- 모듈 수준 API
_INSTANCES: dict[tuple[str, str], KnowledgeBase] = {}


def get_knowledge_base(kb_id: str = "default", settings: RAGSettings | None = None) -> KnowledgeBase:
    """kb_id 별 KnowledgeBase 를 재사용한다(설정을 넘기면 새로 만든다)."""
    if settings is not None:
        return KnowledgeBase(kb_id, settings=settings)
    key = (str(RAGSettings().chroma_dir), kb_id)
    with _CACHE_LOCK:
        if key not in _INSTANCES:
            _INSTANCES[key] = KnowledgeBase(kb_id)
        return _INSTANCES[key]


def search_documents(query: str, top_k: int = 5, kb_id: str = "default", **kwargs) -> list[SearchResult]:
    """AI 에이전트가 호출하는 검색 함수. 예) search_documents("홈페이지 담당자", kb_id=session_id)"""
    return get_knowledge_base(kb_id).search(query, top_k=top_k, **kwargs)


def ingest_file(path: str | Path, kb_id: str = "default", **kwargs) -> IngestResult:
    return get_knowledge_base(kb_id).add_file(path, **kwargs)


def ingest_url(url: str, kb_id: str = "default", **kwargs) -> IngestResult:
    return get_knowledge_base(kb_id).add_url(url, **kwargs)
