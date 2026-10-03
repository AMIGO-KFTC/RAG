"""임베딩 함수.

기본값은 외부 모델 다운로드 없이 동작하는 '문자 n-gram 해싱 임베딩'이다.
한국어는 교착어라 어절 단위로는 조사·어미 때문에 일치가 잘 안 되지만, 음절 2-gram/3-gram 은
'홈페이지 담당자' ↔ '홈페이지 운영 담당자는' 같은 표현을 잘 맞춘다. 사내망(인터넷 차단)에서도 바로 쓸 수 있다.

의미 기반 검색 품질을 높이려면 sentence-transformers 를 설치하고
AMIGO_RAG_EMBEDDING=st:jhgan/ko-sroberta-multitask (또는 BAAI/bge-m3 등) 로 지정한다.
"""

from __future__ import annotations

import math
import re
import unicodedata
import zlib
from typing import Protocol

import numpy as np

_TOKEN_RE = re.compile(r"[가-힣]+|[a-zA-Z][a-zA-Z0-9_]*|\d+")
_HANGUL_RE = re.compile(r"^[가-힣]+$")


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def tokenize(text: str) -> list[str]:
    """검색용 토큰: 영문/숫자 단어 + 한글 어절 + 한글 음절 2-gram.

    BM25 키워드 검색과 해싱 임베딩이 같은 토크나이저를 공유한다.
    """
    text = unicodedata.normalize("NFC", text or "").lower()
    tokens: list[str] = []
    for word in _TOKEN_RE.findall(text):
        if _HANGUL_RE.match(word):
            if len(word) == 1:
                tokens.append(word)
                continue
            tokens.append(word)
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
        elif len(word) > 1 or word.isdigit():
            tokens.append(word)
    return tokens


class HashingEmbedder:
    """음절 n-gram + 단어를 고정 차원 벡터로 해싱하는 오프라인 임베딩."""

    name = "hash-ngram-v1"

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def _features(self, text: str) -> dict[str, float]:
        feats: dict[str, float] = {}
        text = unicodedata.normalize("NFC", text or "").lower()
        for word in _TOKEN_RE.findall(text):
            if _HANGUL_RE.match(word):
                feats["w:" + word] = feats.get("w:" + word, 0) + 1.0
                padded = f"<{word}>"
                for n in (2, 3):
                    for i in range(len(padded) - n + 1):
                        gram = padded[i : i + n]
                        if gram.strip("<>"):
                            feats["g:" + gram] = feats.get("g:" + gram, 0) + 1.0
            else:
                feats["w:" + word] = feats.get("w:" + word, 0) + 1.5
        return feats

    def _embed(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        for feat, count in self._features(text).items():
            h = zlib.crc32(feat.encode("utf-8"))
            sign = 1.0 if (h >> 31) & 1 else -1.0
            vec[h % self.dim] += sign * (1.0 + math.log(count))
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        else:
            vec[0] = 1.0  # 빈 텍스트도 유효한 벡터가 되도록
        return vec.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class SentenceTransformerEmbedder:
    """sentence-transformers 모델 래퍼(선택 의존성). e5 계열은 query:/passage: 접두어를 자동으로 붙인다."""

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers 가 설치되어 있지 않습니다. 'pip install amigo-rag[st]' 후 다시 시도하세요."
            ) from exc
        self.model = SentenceTransformer(model_name)
        self.name = "st:" + model_name
        self._e5 = "e5" in model_name.lower()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._e5:
            texts = ["passage: " + t for t in texts]
        return self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()

    def embed_query(self, text: str) -> list[float]:
        if self._e5:
            text = "query: " + text
        return self.model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0].tolist()


_CACHE: dict[str, Embedder] = {}


def get_embedder(spec: str = "hash") -> Embedder:
    """'hash' 또는 'st:<모델명>' 형식의 설정 문자열로 임베더를 만든다(프로세스 내 캐시)."""
    spec = (spec or "hash").strip()
    if spec not in _CACHE:
        if spec in ("hash", "default", ""):
            _CACHE[spec] = HashingEmbedder()
        elif spec.startswith("st:"):
            _CACHE[spec] = SentenceTransformerEmbedder(spec[3:])
        else:
            raise ValueError(f"알 수 없는 임베딩 설정입니다: {spec} (hash 또는 st:<모델명>)")
    return _CACHE[spec]
