"""동의어 사전: 같은 뜻의 업무 용어를 묶어 두면, 질문과 자료의 단어가 달라도 '관련 있음'으로 본다.

사전은 JSON 파일 하나다(사용자가 직접 고치거나 화면에서 편집한 결과를 저장한다).

    {"groups": [["대금", "비용", "요금"], ["인계자", "전임자"]]}

같은 묶음의 단어는 서로 바꿔 쓸 수 있다. 조각(청크)에 묶음 중 하나가 들어 있으면 나머지 단어도 들어 있는 것처럼 색인한다.
그래서 자료에 '대금 지급'만 있어도 '비용이 얼마인가요' 질문이 그 조각을 찾고 관련 있음으로 판정한다.
사전을 고치면 다음 검색부터 바로 반영된다(자료를 다시 적재할 필요가 없다).
"""

from __future__ import annotations

import json
import os
import threading
import unicodedata
from pathlib import Path


def _norm(term: str) -> str:
    return unicodedata.normalize("NFC", term or "").strip().lower()


class SynonymDict:
    def __init__(self, groups: list[list[str]] | None = None, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._groups: list[list[str]] = []
        self._index: dict[str, int] = {}
        self._version = 0
        self._mtime: float | None = None
        self.set_groups(groups or [])

    # ------------------------------------------------------------------ 읽기·쓰기
    @classmethod
    def load(cls, path: str | os.PathLike | None) -> "SynonymDict":
        """파일이 없거나 비어 있으면 빈 사전. 형식이 잘못되면 ValueError."""
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            groups = data["groups"] if isinstance(data, dict) else data
            result = cls([[str(t) for t in g] for g in groups], path=p)
            result._mtime = p.stat().st_mtime
            return result
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"동의어 사전 형식이 올바르지 않습니다({p}): {exc}") from exc

    def save(self) -> None:
        if not self.path:
            raise ValueError("저장할 경로가 없습니다.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"groups": self.groups}, ensure_ascii=False, indent=2), encoding="utf-8")

    def refresh(self) -> None:
        """파일이 바뀌었으면 다시 읽는다. 형식이 잘못된 파일은 무시하고 지금 사전을 유지한다."""
        if not self.path or not self.path.exists():
            return
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        try:
            fresh = SynonymDict.load(self.path)
        except ValueError:
            self._mtime = mtime
            return
        self._mtime = mtime
        if fresh.groups != self.groups:
            self.set_groups(fresh.groups)

    @property
    def signature(self) -> tuple:
        """내용이 같으면 같은 값(검색 색인 캐시 키)."""
        return tuple(tuple(g) for g in self._groups)

    @property
    def groups(self) -> list[list[str]]:
        return [list(g) for g in self._groups]

    @property
    def version(self) -> int:
        """내용이 바뀔 때마다 늘어난다(검색 색인 캐시를 새로 만들 때 쓴다)."""
        return self._version

    def __bool__(self) -> bool:
        return bool(self._groups)

    # ------------------------------------------------------------------ 편집
    def set_groups(self, groups: list[list[str]]) -> None:
        cleaned: list[list[str]] = []
        for g in groups:
            terms = list(dict.fromkeys(t for t in (_norm(x) for x in g) if t))
            if len(terms) >= 2:
                cleaned.append(terms)
        with self._lock:
            # 여러 묶음에 같은 단어가 있으면 하나로 합친다(A=B, B=C 이면 A=B=C).
            merged: list[set[str]] = []
            for terms in cleaned:
                hit = [m for m in merged if m & set(terms)]
                union = set(terms).union(*hit) if hit else set(terms)
                merged = [m for m in merged if m not in hit] + [union]
            self._groups = [sorted(m) for m in merged]
            self._index = {t: i for i, g in enumerate(self._groups) for t in g}
            self._version += 1

    def add_group(self, terms: list[str]) -> None:
        self.set_groups(self.groups + [terms])

    def remove_term(self, term: str) -> None:
        """단어 하나를 사전에서 뺀다(묶음에 단어가 하나만 남으면 묶음도 사라진다)."""
        t = _norm(term)
        self.set_groups([[x for x in g if x != t] for g in self._groups])

    # ------------------------------------------------------------------ 확장
    def expand(self, text: str) -> str:
        """text 에 들어 있는 사전 단어의 동의어를 이어 붙인 문자열(없으면 빈 문자열)."""
        if not self._groups or not text:
            return ""
        low = unicodedata.normalize("NFC", text).lower()
        extra: list[str] = []
        seen: set[int] = set()
        for term, gi in self._index.items():
            if gi not in seen and term in low:
                seen.add(gi)
                extra.extend(t for t in self._groups[gi] if t not in low)
        return " ".join(extra)
