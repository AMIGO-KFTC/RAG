"""HWPX(한글 OWPML) 파서.

HWPX 는 ZIP 안에 XML 이 들어 있는 형식이다.
  - Contents/content.hpf : 섹션 순서(spine)와 문서 메타데이터
  - Contents/header.xml  : 스타일 정의(개요 1, 개요 2 … → 제목 수준 판별에 사용)
  - Contents/section0.xml, section1.xml … : 본문(hp:p 문단, hp:tbl 표, hp:t 텍스트)
  - Preview/PrvText.txt  : 미리보기 텍스트(본문 해석 실패 시 대체용)
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path, PurePosixPath

from lxml import etree

from ..models import Block, ParsedDocument, ParseError
from ..textutil import clean_text, decode_bytes, heading_level_from_style, table_to_markdown

MAX_XML_BYTES = 64 * 1024 * 1024
_SKIP_TAGS = {"secPr", "colPr", "header", "footer", "pageNum", "pageHiding", "linesegarray", "parameters"}


def _safe_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True, recover=True)


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def parse_hwpx(path: Path) -> ParsedDocument:
    doc = ParsedDocument(source_name=path.name, source_type="hwpx")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ParseError("HWPX 파일이 손상되었거나 HWPX 형식이 아닙니다. (구버전 .hwp 라면 확장자를 확인해 주세요)") from exc

    with zf:
        names = set(zf.namelist())
        if not any(n.startswith("Contents/") for n in names):
            raise ParseError("HWPX 구조(Contents/)를 찾을 수 없습니다.")

        styles = _read_styles(zf, names)
        sections = _section_order(zf, names, doc)
        for section_name in sections:
            root = _read_xml(zf, section_name)
            if root is None:
                continue
            for child in root:
                if _local(child.tag) == "p":
                    doc.blocks.extend(_paragraph_blocks(child, styles))

        if not any(b.text.strip() for b in doc.blocks):
            preview = _read_preview(zf, names)
            if preview:
                doc.warnings.append("본문 구조를 해석하지 못해 미리보기 텍스트로 대체했습니다.")
                doc.blocks = [Block(p, kind="paragraph") for p in preview.split("\n\n") if p.strip()]
    return doc


def _read_xml(zf: zipfile.ZipFile, name: str):
    info = zf.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ParseError(f"HWPX 내부 파일이 너무 큽니다: {name}")
    with zf.open(name) as fh:
        data = fh.read(MAX_XML_BYTES + 1)
    try:
        return etree.fromstring(data, parser=_safe_parser())
    except etree.XMLSyntaxError:
        return None


def _section_order(zf: zipfile.ZipFile, names: set[str], doc: ParsedDocument) -> list[str]:
    """content.hpf 의 spine 순서대로 섹션 파일 경로를 반환. 없으면 section 번호순."""
    ordered: list[str] = []
    if "Contents/content.hpf" in names:
        root = _read_xml(zf, "Contents/content.hpf")
        if root is not None:
            manifest = {}
            for el in root.iter():
                tag = _local(el.tag)
                if tag == "item" and el.get("id") and el.get("href"):
                    manifest[el.get("id")] = el.get("href")
                elif tag == "title" and (el.text or "").strip():
                    doc.metadata.setdefault("title", clean_text(el.text))
                elif tag == "meta" and el.get("name") in ("creator", "lastsaveby") and (el.text or "").strip():
                    key = "author" if el.get("name") == "creator" else "last_modified_by"
                    doc.metadata.setdefault(key, clean_text(el.text))
                elif tag == "meta" and el.get("name") in ("CreatedDate", "ModifiedDate") and (el.text or "").strip():
                    key = "created_at" if el.get("name") == "CreatedDate" else "modified_at"
                    doc.metadata.setdefault(key, el.text.strip()[:10])
            for el in root.iter():
                if _local(el.tag) == "itemref":
                    href = manifest.get(el.get("idref", ""))
                    if href and "section" in href.lower():
                        resolved = _resolve_href(href, names)
                        if resolved:
                            ordered.append(resolved)
    if not ordered:
        candidates = [n for n in names if re.match(r"^Contents/section\d+\.xml$", n)]
        ordered = sorted(candidates, key=lambda n: int(re.findall(r"\d+", n)[-1]))
    return ordered


def _resolve_href(href: str, names: set[str]) -> str | None:
    href = href.lstrip("/")
    for candidate in (href, str(PurePosixPath("Contents") / href)):
        if candidate in names:
            return candidate
    return None


def _read_styles(zf: zipfile.ZipFile, names: set[str]) -> dict[str, int]:
    """styleIDRef → 제목 수준. '개요 1'/'Outline 1' 스타일을 제목으로 본다."""
    if "Contents/header.xml" not in names:
        return {}
    root = _read_xml(zf, "Contents/header.xml")
    if root is None:
        return {}
    levels: dict[str, int] = {}
    for el in root.iter():
        if _local(el.tag) == "style" and el.get("id") is not None:
            level = heading_level_from_style(el.get("name")) or heading_level_from_style(el.get("engName"))
            if level:
                levels[el.get("id")] = level
    return levels


def _paragraph_blocks(p, styles: dict[str, int]) -> list[Block]:
    """hp:p 하나를 블록 목록으로 변환. 문단 안에 표/글상자가 있으면 별도 블록으로 분리한다."""
    blocks: list[Block] = []
    buffer: list[str] = []

    def flush():
        text = clean_text("".join(buffer))
        buffer.clear()
        if text:
            level = styles.get(p.get("styleIDRef", ""), 0)
            if level and len(text) <= 120:
                blocks.append(Block(text, kind="heading", level=level))
            else:
                blocks.append(Block(text, kind="paragraph"))

    def walk(node):
        for child in node:
            tag = _local(child.tag)
            if tag in _SKIP_TAGS:
                continue
            if tag == "t":
                buffer.append(_text_of_t(child))
            elif tag == "tbl":
                flush()
                table = _table_markdown(child, styles)
                if table:
                    blocks.append(Block(table, kind="table"))
            elif tag == "p":  # 글상자 등 안쪽 문단
                flush()
                blocks.extend(_paragraph_blocks(child, styles))
            else:
                walk(child)

    walk(p)
    flush()
    return blocks


def _text_of_t(t) -> str:
    parts = [t.text or ""]
    for child in t:
        tag = _local(child.tag)
        if tag == "tab":
            parts.append("\t")
        elif tag == "lineBreak":
            parts.append("\n")
        elif tag in ("fwSpace", "nbSpace"):
            parts.append(" ")
        elif tag == "hyphen":
            parts.append("-")
        parts.append(child.tail or "")
    return "".join(parts)


def _table_markdown(tbl, styles: dict[str, int]) -> str:
    rows: list[list[str]] = []
    for tr in tbl:
        if _local(tr.tag) != "tr":
            continue
        row: list[str] = []
        for tc in tr:
            if _local(tc.tag) != "tc":
                continue
            texts = []
            for block in _cell_blocks(tc, styles):
                texts.append(block.text.replace("\n", " "))
            row.append(" ".join(t for t in texts if t))
            span = 1
            for el in tc:
                if _local(el.tag) == "cellSpan":
                    try:
                        span = max(1, int(el.get("colSpan", "1")))
                    except ValueError:
                        span = 1
            row.extend([""] * (span - 1))
        rows.append(row)
    return table_to_markdown(rows)


def _cell_blocks(tc, styles: dict[str, int]) -> list[Block]:
    blocks: list[Block] = []
    for el in tc.iter():
        if _local(el.tag) == "subList":
            for p in el:
                if _local(p.tag) == "p":
                    blocks.extend(_paragraph_blocks(p, styles))
            break
    return blocks


def _read_preview(zf: zipfile.ZipFile, names: set[str]) -> str:
    for name in ("Preview/PrvText.txt", "Preview/PrvText"):
        if name in names:
            with zf.open(name) as fh:
                data = fh.read(4 * 1024 * 1024)
            return clean_text(decode_bytes(data))
    return ""
