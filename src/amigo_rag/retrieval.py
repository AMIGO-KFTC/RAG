"""하이브리드 검색: 벡터 유사도(ChromaDB) + BM25 키워드 점수를 RRF(Reciprocal Rank Fusion)로 합친다.

임베딩만으로는 사람 이름·시스템명·약어 같은 고유명사를 놓치기 쉽고, 키워드만으로는 표현이 다른 문장을 놓친다.
두 순위를 합치면 '홈페이지 담당자', 'ERP 권한 신청' 같은 업무 질의에서 안정적으로 결과가 나온다.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from .embeddings import tokenize


@dataclass
class _Doc:
    id: str
    text: str
    metadata: dict
    tokens: Counter
    length: int


class BM25Index:
    """지식베이스 하나에 대한 메모리 BM25 색인(문서 수천 개 규모의 프로토타입용)."""

    def __init__(self, rows: list[dict], k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs: list[_Doc] = []
        df: Counter = Counter()
        for row in rows:
            meta = row.get("metadata") or {}
            context = " ".join(str(meta.get(k, "")) for k in ("file_name", "section", "subject", "title"))
            tokens = Counter(tokenize(context + " " + (row.get("text") or "")))
            self.docs.append(_Doc(row["id"], row.get("text") or "", meta, tokens, sum(tokens.values())))
            df.update(tokens.keys())
        n = max(len(self.docs), 1)
        self.avgdl = sum(d.length for d in self.docs) / n if self.docs else 0.0
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, top_k: int, where: dict | None = None) -> list[tuple[_Doc, float]]:
        q_tokens = [t for t in set(tokenize(query)) if t in self.idf]
        if not q_tokens:
            return []
        scored = []
        for doc in self.docs:
            if where and not matches_where(doc.metadata, where):
                continue
            score = 0.0
            for t in q_tokens:
                tf = doc.tokens.get(t, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * doc.length / (self.avgdl or 1))
                score += self.idf[t] * tf * (self.k1 + 1) / denom
            if score > 0:
                scored.append((doc, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def matches_where(meta: dict, where: dict) -> bool:
    """Chroma where 문법 중 자주 쓰는 부분($and/$or/$eq/$ne/$in/$nin/단순 일치)을 메모리에서 평가한다."""
    for key, cond in where.items():
        if key == "$and":
            if not all(matches_where(meta, c) for c in cond):
                return False
            continue
        if key == "$or":
            if not any(matches_where(meta, c) for c in cond):
                return False
            continue
        value = meta.get(key)
        if isinstance(cond, dict):
            for op, expected in cond.items():
                if op == "$eq" and value != expected:
                    return False
                if op == "$ne" and value == expected:
                    return False
                if op == "$in" and value not in expected:
                    return False
                if op == "$nin" and value in expected:
                    return False
        elif value != cond:
            return False
    return True


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60, weights: list[float] | None = None) -> dict[str, float]:
    weights = weights or [1.0] * len(rankings)
    fused: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + rank + 1)
    return fused
