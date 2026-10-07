"""청킹(Chunking) 및 메타데이터 부착.

원칙
  1. 문서 구조를 존중한다: 제목(heading)이 나오면 새 청크를 시작하고, 제목 경로를 section 메타데이터로 남긴다.
  2. 표는 가능한 한 통째로 유지하고, 너무 크면 머리글 행을 반복하면서 행 단위로 나눈다.
  3. 일반 문단은 chunk_size(문자 수) 안에서 묶고, 넘칠 때는 문장 단위로 자른 뒤 앞 청크의 끝부분을 overlap 만큼 겹친다.
  4. 모든 청크에 출처 메타데이터(파일명, 페이지, 섹션, 발신자, 일시, URL …)를 붙인다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Block, Chunk, ParsedDocument

# 문서 수준 메타데이터 중 청크에 복사할 키
_DOC_META_KEYS = (
    "title", "author", "created_at", "modified_at", "sender", "sender_name", "recipients", "cc", "sent_at",
    "subject", "url", "space", "language", "path_in_archive", "last_modified_by",
)
# 메일 첨부파일 청크가 물려받을 메일 메타데이터
_INHERITED_KEYS = ("sender", "sender_name", "sent_at", "subject", "recipients", "url", "space")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。…])\s+|(?<=다\.)|\n+")


@dataclass
class _Piece:
    texts: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    sheet: str | None = None
    section: str = ""

    @property
    def length(self) -> int:
        return sum(len(t) for t in self.texts) + 2 * max(len(self.texts) - 1, 0)

    def add(self, text: str, kind: str, page: int | None, sheet: str | None = None) -> None:
        self.texts.append(text)
        self.kinds.append(kind)
        if page:
            self.pages.append(page)
        if sheet and not self.sheet:
            self.sheet = sheet

    @property
    def has_body(self) -> bool:
        return any(k != "heading" for k in self.kinds)


def chunk_document(
    doc: ParsedDocument,
    *,
    source_id: str,
    chunk_size: int = 900,
    chunk_overlap: int = 150,
    extra_metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """ParsedDocument(첨부 포함)를 Chunk 목록으로 변환한다."""
    chunk_size = max(chunk_size, 200)
    chunk_overlap = max(0, min(chunk_overlap, chunk_size // 3))
    chunks: list[Chunk] = []
    _chunk_part(doc, doc, None, source_id, chunk_size, chunk_overlap, extra_metadata or {}, chunks)
    return chunks


def _chunk_part(root, part, parent, source_id, size, overlap, extra, out: list[Chunk]) -> None:
    base = _base_metadata(root, part, parent, source_id, extra)
    for piece in _pieces(part.blocks, size, overlap):
        text = "\n\n".join(piece.texts).strip()
        if not text:
            continue
        meta = dict(base)
        meta["chunk_index"] = len(out)
        meta["char_count"] = len(text)
        meta["kinds"] = ",".join(sorted(set(piece.kinds) - {"overlap"}))
        if piece.section:
            meta["section"] = piece.section[:300]
        if piece.pages:
            meta["page"] = min(piece.pages)
            if max(piece.pages) != min(piece.pages):
                meta["page_end"] = max(piece.pages)
        if piece.sheet:
            meta["sheet"] = piece.sheet
        out.append(Chunk(id=f"{source_id}:{len(out):04d}", text=text, metadata=_primitive(meta)))
    for attachment in part.attachments:
        _chunk_part(root, attachment, part, source_id, size, overlap, extra, out)


def _base_metadata(root: ParsedDocument, part: ParsedDocument, parent, source_id: str, extra: dict) -> dict:
    meta: dict[str, Any] = {
        "source_id": source_id,
        "source_name": root.source_name,
        "source_type": root.source_type,
        "file_name": part.source_name,
        "file_type": part.source_type,
    }
    for key in _DOC_META_KEYS:
        value = part.metadata.get(key)
        if value not in (None, ""):
            meta[key] = value
    if parent is not None:
        meta["parent_name"] = part.metadata.get("parent_name") or parent.source_name
        for key in _INHERITED_KEYS:
            if key not in meta and parent.metadata.get(key):
                meta[key] = parent.metadata[key]
    for key, value in extra.items():
        meta.setdefault(key, value)
    return meta


def _pieces(blocks: list[Block], size: int, overlap: int):
    """블록 목록을 청크 후보(_Piece)로 묶는다."""
    headings: list[tuple[int, str]] = []
    current = _Piece()

    def section() -> str:
        return " > ".join(text for _, text in headings)

    def emit(piece: _Piece):
        if piece.texts and piece.has_body:
            yield piece

    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        sheet = block.meta.get("sheet") if block.meta else None

        if block.kind == "heading":
            level = block.level or 1
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text[:120]))
            if current.has_body:
                yield from emit(current)
                current = _Piece()
            current.section = section()  # 아직 본문이 없으면 더 깊은 제목 경로로 갱신
            current.add("#" * min(level, 6) + " " + text, "heading", block.page, sheet)
            continue

        if block.kind == "table":
            if len(text) > size * 1.5:
                yield from emit(current)
                for part in _split_table(text, size):
                    piece = _Piece(section=section())
                    piece.add(part, "table", block.page, sheet)
                    yield piece
                current = _Piece(section=section())
                continue
            if current.length + len(text) > size and current.has_body:
                yield from emit(current)
                current = _Piece(section=section())
            if not current.texts:
                current.section = section()
            current.add(text, "table", block.page, sheet)
            continue

        if len(text) > size:
            yield from emit(current)
            splitter = _split_code if block.kind == "code" else _split_long_text
            for part in splitter(text, size, overlap):
                piece = _Piece(section=section())
                piece.add(part, block.kind, block.page, sheet)
                yield piece
            current = _Piece(section=section())
            continue

        if current.length + len(text) + 2 > size and current.has_body:
            tail = _tail(current, overlap)
            yield from emit(current)
            current = _Piece(section=section())
            if tail:
                current.add(tail, "overlap", None)
        if not current.texts:
            current.section = section()
        current.add(text, block.kind, block.page, sheet)

    yield from emit(current)


def _tail(piece: _Piece, overlap: int) -> str:
    """앞 청크의 마지막 문장들(최대 overlap 자)을 다음 청크 앞에 붙여 문맥을 잇는다."""
    if overlap <= 0:
        return ""
    for text, kind in zip(reversed(piece.texts), reversed(piece.kinds)):
        if kind in ("table", "heading", "code", "meta", "overlap"):
            return ""
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s and s.strip()]
        tail: list[str] = []
        total = 0
        for sentence in reversed(sentences):
            if total + len(sentence) > overlap:
                break
            tail.insert(0, sentence)
            total += len(sentence) + 1
        return " ".join(tail)
    return ""


def _split_long_text(text: str, size: int, overlap: int) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s and s.strip()]
    parts: list[str] = []
    buf: list[str] = []
    length = 0
    for sentence in sentences:
        while len(sentence) > size:  # 한 문장이 너무 길면 강제로 자른다
            if buf:
                parts.append(" ".join(buf))
                buf, length = [], 0
            parts.append(sentence[:size])
            sentence = sentence[max(size - overlap, 1):]
        if length + len(sentence) + 1 > size and buf:
            parts.append(" ".join(buf))
            keep: list[str] = []
            kept = 0
            for prev in reversed(buf):
                if kept + len(prev) > overlap:
                    break
                keep.insert(0, prev)
                kept += len(prev) + 1
            buf, length = keep, kept
        buf.append(sentence)
        length += len(sentence) + 1
    if buf:
        parts.append(" ".join(buf))
    return [p for p in parts if p.strip()]


def _split_code(text: str, size: int, overlap: int) -> list[str]:
    """코드는 줄 단위로 묶어 들여쓰기·줄바꿈을 보존한다."""
    parts: list[str] = []
    buf: list[str] = []
    length = 0
    for line in text.split("\n"):
        line = line[: size]
        if buf and length + len(line) + 1 > size:
            parts.append("\n".join(buf))
            keep: list[str] = []
            kept = 0
            for prev in reversed(buf):
                if kept + len(prev) + 1 > overlap:
                    break
                keep.insert(0, prev)
                kept += len(prev) + 1
            buf, length = keep, kept
        buf.append(line)
        length += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return [p for p in parts if p.strip()]


def _split_table(markdown: str, size: int) -> list[str]:
    lines = markdown.split("\n")
    if len(lines) < 3 or not lines[1].startswith("|"):
        return _split_long_text(markdown, size, 0)
    header = lines[:2]
    header_len = sum(len(h) + 1 for h in header)
    parts: list[str] = []
    rows: list[str] = []
    length = header_len
    for row in lines[2:]:
        if rows and length + len(row) + 1 > size:
            parts.append("\n".join(header + rows))
            rows, length = [], header_len
        rows.append(row[: size * 2])
        length += len(row) + 1
    if rows:
        parts.append("\n".join(header + rows))
    return parts


def _primitive(meta: dict[str, Any]) -> dict[str, Any]:
    """ChromaDB 메타데이터는 str/int/float/bool 만 허용된다."""
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None or value == "":
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, (list, tuple, set)):
            out[key] = ", ".join(str(v) for v in value)
        else:
            out[key] = str(value)
    return out
