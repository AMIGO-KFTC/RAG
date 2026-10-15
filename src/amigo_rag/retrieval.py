"""하이브리드 검색: 벡터 유사도(ChromaDB) + BM25 키워드 점수를 RRF(Reciprocal Rank Fusion)로 합친다.

임베딩만으로는 사람 이름·시스템명·약어 같은 고유명사를 놓치기 쉽고, 키워드만으로는 표현이 다른 문장을 놓친다.
두 순위를 합치면 '홈페이지 담당자', 'ERP 권한 신청' 같은 업무 질의에서 안정적으로 결과가 나온다.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from .embeddings import tokenize

# ---- "이 조각이 질문과 정말 관련 있나"를 가늠하는 단위(content_units)
# 질문 속 '인가요/하나요' 같은 어미·조사와 '어떻게/언제' 같은 의문사는 어떤 문서에나 있거나 없어서 판단에 도움이 안 되므로 뺀다.
_UNIT_WORD = re.compile(r"[가-힣]+|[a-zA-Z][a-zA-Z0-9_]*|\d+")
_HANGUL_ONLY = re.compile(r"^[가-힣]+$")
_SUFFIXES = tuple(
    sorted(
        "인가요 입니까 이에요 입니다 하는지 한가요 되나요 있나요 했나요 하나요 나요 인가 했나 하나 받나 나면 "
        "에게 에서는 에서 까지 부터 으로 에는 에도 이란 인지 이며".split(),
        key=len,
        reverse=True,
    )
)
_ONE_CHAR_SUFFIXES = tuple("은는이가을를에의로도와과만")
_QUESTION_WORDS = frozenset("어떻게 언제 누구 누가 얼마 어디 어디에 무엇 무슨 몇 어떤 방법 절차 알려줘 알려 주세요".split())
_LIGHT_SYLLABLES = frozenset("하되있없안누")


def _strip_suffix(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) > len(suffix):
            return word[: -len(suffix)]
    # 한 글자 조사는 남는 말이 두 글자 이상일 때만 뗀다('회의'가 '회'가 되는 것을 막는다)
    if len(word) >= 3 and word[-1] in _ONE_CHAR_SUFFIXES:
        return word[:-1]
    return word


def content_units(text: str) -> set[str]:
    """관련도 판정용 단위: 영문/숫자 단어 + 한글 음절 2-gram(어미·조사·의문사 제거 후)."""
    units: set[str] = set()
    for word in _UNIT_WORD.findall(unicodedata.normalize("NFC", text or "").lower()):
        if _HANGUL_ONLY.match(word):
            word = _strip_suffix(word)
            if not word or word in _QUESTION_WORDS or word in _LIGHT_SYLLABLES:
                continue
            if len(word) == 1:
                units.add(word)
            else:
                units.update(word[i : i + 2] for i in range(len(word) - 1))
        elif len(word) > 1 or word.isdigit():
            units.add(word)
    return units


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
        self._units: dict[str, set[str]] = {}
        unit_df: Counter = Counter()
        for row in rows:
            meta = row.get("metadata") or {}
            context = " ".join(str(meta.get(k, "")) for k in ("file_name", "section", "subject", "title"))
            tokens = Counter(tokenize(context + " " + (row.get("text") or "")))
            self.docs.append(_Doc(row["id"], row.get("text") or "", meta, tokens, sum(tokens.values())))
            df.update(tokens.keys())
            units = content_units(context + " " + (row.get("text") or ""))
            self._units[row["id"]] = units
            unit_df.update(units)
        n = max(len(self.docs), 1)
        # 드문 단어일수록 큰 비중. 지식베이스 어디에도 없는 단어(df=0)가 가장 크다.
        self._unit_df = unit_df
        self._unit_n = n
        self.avgdl = sum(d.length for d in self.docs) / n if self.docs else 0.0
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def relevance(self, query_units: set[str], doc_id: str) -> float:
        """질문의 핵심 단위 중 이 조각이 가진 것의 비중(0~1, 드문 단위일수록 가중). 질문에 쓸 단위가 없으면 0."""
        if not query_units:
            return 0.0
        have = self._units.get(doc_id, set())
        total = matched = 0.0
        for unit in query_units:
            weight = math.log(1 + self._unit_n / (self._unit_df.get(unit, 0) + 0.5))
            total += weight
            if unit in have:
                matched += weight
        return matched / total if total else 0.0

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
