"""ChromaDB 저장소 래퍼.

지식베이스(kb_id) 하나가 Chroma 컬렉션 하나에 대응한다. 인수인계 세션마다 별도의 지식베이스를 쓰므로
세션을 지우면 컬렉션을 통째로 삭제하면 된다. 임베딩은 우리가 직접 계산해서 넣는다(embedding_function=None).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
from chromadb.config import Settings as ChromaSettings

_CLIENTS: dict[str, Any] = {}
_LOCK = threading.Lock()


def _client(persist_dir: Path | None):
    key = str(Path(persist_dir).resolve()) if persist_dir else ":memory:"
    with _LOCK:
        if key not in _CLIENTS:
            settings = ChromaSettings(anonymized_telemetry=False, allow_reset=True)
            if persist_dir:
                Path(persist_dir).mkdir(parents=True, exist_ok=True)
                _CLIENTS[key] = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
            else:
                _CLIENTS[key] = chromadb.EphemeralClient(settings=settings)
        return _CLIENTS[key]


def collection_name(kb_id: str) -> str:
    """Chroma 컬렉션 이름 규칙(3~512자, 영숫자/._-, 영숫자로 시작·끝)에 맞춘다."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", kb_id or "default").strip("-._") or "default"
    return f"kb_{safe}"[:500]


class ChromaStore:
    def __init__(self, persist_dir: Path | None, embedder_name: str) -> None:
        self.client = _client(persist_dir)
        self.embedder_name = embedder_name

    def collection(self, kb_id: str, create: bool = True):
        name = collection_name(kb_id)
        if not create:
            try:
                return self.client.get_collection(name)
            except Exception:
                return None
        col = self.client.get_or_create_collection(
            name,
            embedding_function=None,
            metadata={"kb_id": kb_id, "embedder": self.embedder_name},
            configuration={"hnsw": {"space": "cosine"}},
        )
        stored = (col.metadata or {}).get("embedder")
        if stored and stored != self.embedder_name:
            raise RuntimeError(
                f"지식베이스 '{kb_id}' 는 '{stored}' 임베딩으로 만들어졌습니다. 현재 설정('{self.embedder_name}')과 달라 "
                "검색할 수 없습니다. 임베딩 설정을 되돌리거나 지식베이스를 다시 만드세요."
            )
        return col

    def add(self, kb_id: str, ids: list[str], texts: list[str], embeddings: list[list[float]], metadatas: list[dict]) -> None:
        col = self.collection(kb_id)
        batch = 256
        for start in range(0, len(ids), batch):
            end = start + batch
            col.upsert(
                ids=ids[start:end],
                documents=texts[start:end],
                embeddings=embeddings[start:end],
                metadatas=metadatas[start:end],
            )

    def query(self, kb_id: str, embedding: list[float], n_results: int, where: dict | None = None) -> list[dict]:
        col = self.collection(kb_id, create=False)
        if col is None:
            return []
        count = col.count()
        if count == 0:
            return []
        result = col.query(
            query_embeddings=[embedding],
            n_results=min(n_results, count),
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        rows = []
        for cid, doc, meta, dist in zip(
            result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            rows.append({"id": cid, "text": doc, "metadata": dict(meta or {}), "similarity": 1.0 - float(dist)})
        return rows

    def get(self, kb_id: str, where: dict | None = None) -> list[dict]:
        col = self.collection(kb_id, create=False)
        if col is None:
            return []
        result = col.get(where=where or None, include=["documents", "metadatas"])
        return [
            {"id": cid, "text": doc, "metadata": dict(meta or {})}
            for cid, doc, meta in zip(result["ids"], result["documents"], result["metadatas"])
        ]

    def similarities(self, kb_id: str, ids: list[str], query_embedding: list[float]) -> dict[str, float]:
        """키워드 검색으로만 찾은 청크의 벡터 유사도를 계산한다(코사인)."""
        col = self.collection(kb_id, create=False)
        if col is None or not ids:
            return {}
        result = col.get(ids=ids, include=["embeddings"])
        query = np.asarray(query_embedding, dtype=np.float32)
        q_norm = float(np.linalg.norm(query)) or 1.0
        sims: dict[str, float] = {}
        for cid, emb in zip(result["ids"], result["embeddings"]):
            vec = np.asarray(emb, dtype=np.float32)
            sims[cid] = float(vec @ query) / ((float(np.linalg.norm(vec)) or 1.0) * q_norm)
        return sims

    def count(self, kb_id: str) -> int:
        col = self.collection(kb_id, create=False)
        return col.count() if col is not None else 0

    def delete(self, kb_id: str, where: dict) -> int:
        col = self.collection(kb_id, create=False)
        if col is None:
            return 0
        ids = col.get(where=where, include=[])["ids"]
        if ids:
            col.delete(ids=ids)
        return len(ids)

    def drop(self, kb_id: str) -> None:
        try:
            self.client.delete_collection(collection_name(kb_id))
        except Exception:
            pass
