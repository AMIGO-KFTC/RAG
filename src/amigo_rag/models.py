"""RAG 모듈 전반에서 주고받는 데이터 구조."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class ParseError(Exception):
    """파일/링크를 텍스트로 변환하지 못했을 때 발생한다. 메시지는 사용자에게 그대로 보여줄 수 있게 한국어로 쓴다."""


@dataclass
class Block:
    """문서에서 추출한 구조 단위(문단, 제목, 표 등).

    kind: "heading" | "paragraph" | "table" | "list" | "code" | "image" | "meta"
    level: 제목 수준(1이 가장 상위). heading 이 아니면 0.
    page: 1부터 시작하는 페이지/슬라이드 번호(알 수 없으면 None).
    meta: 블록 단위 부가 정보(예: 엑셀 시트명, 코드 언어).
    """

    text: str
    kind: str = "paragraph"
    level: int = 0
    page: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    """파서가 반환하는 결과. 첨부파일(메일 첨부, ZIP 내부 파일)은 attachments 로 중첩된다."""

    source_name: str
    source_type: str
    blocks: list[Block] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[ParsedDocument] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())

    def iter_all(self):
        """자기 자신과 모든 첨부 문서를 깊이 우선으로 순회한다."""
        yield self
        for att in self.attachments:
            yield from att.iter_all()


@dataclass
class Chunk:
    """벡터DB에 저장되는 검색 단위. metadata 값은 str/int/float/bool 만 허용(ChromaDB 제약)."""

    id: str
    text: str
    metadata: dict[str, Any]


@dataclass
class SearchResult:
    """search_documents() 의 반환 단위."""

    chunk_id: str
    text: str
    score: float
    metadata: dict[str, Any]
    vector_score: float | None = None
    keyword_score: float | None = None

    @property
    def source_id(self) -> str:
        return str(self.metadata.get("source_id", ""))

    @property
    def citation(self) -> str:
        """사람이 읽을 수 있는 출처 표기. 예: "업무정의서.pdf p.3", "메일 '월간보고' (김철수, 2024-03-02)"."""
        return format_citation(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["citation"] = self.citation
        return data


@dataclass
class IngestResult:
    """add_file / add_url / add_text 결과."""

    source_id: str
    source_name: str
    source_type: str
    chunk_count: int
    char_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceInfo:
    """지식베이스에 등록된 원천 자료 요약."""

    source_id: str
    source_name: str
    source_type: str
    chunk_count: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TYPE_LABELS = {
    "eml": "메일",
    "msg": "메일",
    "confluence": "컨플루언스",
    "nanumi": "나누미",
    "web": "웹",
    "note": "메모",
    "chat": "대화",
}


def format_citation(meta: dict[str, Any]) -> str:
    """청크 메타데이터로부터 출처 문자열을 만든다.

    file_type 은 실제 텍스트가 나온 파일(첨부 포함)의 형식, source_type 은 등록된 원천 자료의 형식이다.
    """
    kind = str(meta.get("file_type") or meta.get("source_type") or "")
    name = str(meta.get("file_name") or meta.get("source_name") or "자료")

    if kind in ("eml", "msg"):
        subject = meta.get("subject") or name
        who = meta.get("sender_name") or meta.get("sender") or ""
        when = str(meta.get("sent_at") or "")[:10]
        detail = ", ".join(str(x) for x in (who, when) if x)
        return f"메일 '{subject}'" + (f" ({detail})" if detail else "")

    if kind in ("note", "chat"):
        when = str(meta.get("created_at") or "")[:10]
        return name + (f" ({when})" if when else "")

    parts = [name]
    parent = meta.get("parent_name")
    if parent and parent != name:
        parts = [f"{parent} › {name}"]
    page = meta.get("page")
    page_end = meta.get("page_end")
    if isinstance(page, int) and page > 0 and kind != "xlsx":
        if kind == "pptx":
            span = f"{page}-{page_end}" if isinstance(page_end, int) and page_end > page else f"{page}"
            parts.append(f"슬라이드 {span}")
        else:
            span = f"{page}-{page_end}" if isinstance(page_end, int) and page_end > page else f"{page}"
            parts.append(f"p.{span}")
    if meta.get("sheet"):
        parts.append(f"[{meta['sheet']}]")
    label = _TYPE_LABELS.get(kind)
    if label and kind in ("confluence", "nanumi", "web"):
        parts.insert(0, f"{label}:")
    return " ".join(parts)
