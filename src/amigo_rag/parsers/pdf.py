"""PDF 파서.

- 레이아웃(줄·문단·제목·표 위치)은 pdfplumber 로 분석한다.
- 일부 한글 PDF 는 pdfminer(=pdfplumber 내부)가 특정 글자('·' 등)를 조용히 빠뜨린다. 그래서 페이지마다
  PDFium(크롬 PDF 엔진) 텍스트와 글자 수를 비교해, pdfplumber 쪽이 부족하면 같은 줄/셀 영역의 텍스트를
  PDFium 으로 다시 읽는다(레이아웃은 유지하고 글자만 교체).
- 그래도 '(cid:…)' 처럼 깨지면 PDFium 전체 텍스트로 대체한다.
- 텍스트가 없는 페이지(스캔본)는 경고로 알려준다(OCR 은 별도 확장 지점).
"""

from __future__ import annotations

import statistics
from pathlib import Path

from ..models import Block, ParsedDocument, ParseError
from ..textutil import clean_text, looks_garbled, table_to_markdown


class _PdfiumPage:
    """pdfplumber 좌표(왼쪽 위 원점)로 영역을 지정해 PDFium 텍스트를 읽는 도우미."""

    def __init__(self, pdfium_doc, index: int, height: float) -> None:
        self.textpage = pdfium_doc[index].get_textpage()
        self.height = height

    def full_text(self) -> str:
        return self.textpage.get_text_range()

    def bounded(self, x0: float, top: float, x1: float, bottom: float, pad: float = 0.5) -> str:
        return self.textpage.get_text_bounded(
            left=x0 - pad, bottom=self.height - bottom - pad, right=x1 + pad, top=self.height - top + pad
        )

    def lines(self, exclude: list[tuple[float, float, float, float]]) -> list[dict]:
        """PDFium 글자 상자로 줄을 만든다(PDFium 이 넣는 줄바꿈 표시 기준). exclude 영역(표) 안의 글자는 뺀다."""
        import pypdfium2.raw as pdfium_raw

        textpage = self.textpage
        lines: list[dict] = []
        current: dict | None = None
        for index in range(textpage.count_chars()):
            ch = textpage.get_text_range(index, 1)
            if ch in ("\r", "\n", ""):
                if current:
                    lines.append(current)
                current = None
                continue
            left, bottom, right, top = textpage.get_charbox(index)
            c_top, c_bottom = self.height - top, self.height - bottom
            if ch.strip():
                cx, cy = (left + right) / 2, (c_top + c_bottom) / 2
                if any(x0 <= cx <= x1 and t <= cy <= b for (x0, t, x1, b) in exclude):
                    if current:
                        lines.append(current)
                    current = None
                    continue
            if current is None:
                if not ch.strip():
                    continue
                current = {"text": "", "x0": left, "x1": right, "top": c_top, "bottom": c_bottom, "sizes": []}
            current["text"] += ch
            if ch.strip():
                current["x0"] = min(current["x0"], left)
                current["x1"] = max(current["x1"], right)
                current["top"] = min(current["top"], c_top)
                current["bottom"] = max(current["bottom"], c_bottom)
                current["sizes"].append(pdfium_raw.FPDFText_GetFontSize(textpage.raw, index))
        if current:
            lines.append(current)
        out = []
        for line in lines:
            line["text"] = line["text"].strip()
            if line["text"]:
                line["_size"] = statistics.median(line["sizes"]) if line["sizes"] else 0
                out.append(line)
        out.sort(key=lambda ln: (round(ln["top"] / 3), ln["x0"]))
        return out


def _visible_count(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace() and ch.isprintable() and ch != "￾")


def parse_pdf(path: Path) -> ParsedDocument:
    import pdfplumber

    doc = ParsedDocument(source_name=path.name, source_type="pdf")
    empty_pages: list[int] = []
    pdfium_doc = _open_pdfium(path)
    repaired_pages = 0
    try:
        with pdfplumber.open(str(path)) as pdf:
            info = pdf.metadata or {}
            doc.metadata.update(
                {
                    "title": _meta_str(info.get("Title")),
                    "author": _meta_str(info.get("Author")),
                    "created_at": _pdf_date(info.get("CreationDate")),
                    "page_count": len(pdf.pages),
                }
            )
            for page_no, page in enumerate(pdf.pages, start=1):
                reader = None
                if pdfium_doc is not None:
                    candidate = _PdfiumPage(pdfium_doc, page_no - 1, float(page.height))
                    plumber_count = sum(1 for c in page.chars if (c.get("text") or "").strip())
                    if _visible_count(candidate.full_text()) > plumber_count:
                        reader = candidate
                        repaired_pages += 1
                blocks = _page_blocks(page, page_no, reader)
                if blocks and looks_garbled(" ".join(b.text for b in blocks if b.kind != "table")):
                    blocks = _pdfium_page_blocks(pdfium_doc, page_no) or blocks
                if not any(b.kind != "image" for b in blocks):
                    empty_pages.append(page_no)
                doc.blocks.extend(blocks)
    except ParseError:
        raise
    except Exception as exc:  # pdfminer 는 손상 파일에서 다양한 예외를 던진다
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise ParseError("암호가 설정된 PDF 는 읽을 수 없습니다. 암호를 해제한 뒤 다시 올려 주세요.") from exc
        raise ParseError(f"PDF 를 읽지 못했습니다: {exc}") from exc
    finally:
        if pdfium_doc is not None:
            pdfium_doc.close()

    if repaired_pages:
        doc.metadata["text_engine"] = "pdfplumber+pdfium"
    if empty_pages:
        pages = ", ".join(map(str, empty_pages[:10])) + (" …" if len(empty_pages) > 10 else "")
        doc.warnings.append(f"텍스트가 없는 페이지가 있습니다(스캔 이미지로 추정, OCR 필요): {pages}")
    return doc


def _open_pdfium(path: Path):
    try:
        import pypdfium2 as pdfium

        return pdfium.PdfDocument(str(path))
    except Exception:
        return None


def _page_blocks(page, page_no: int, reader: _PdfiumPage | None) -> list[Block]:
    try:
        tables = [t for t in page.find_tables() if t.bbox]
    except Exception:
        tables = []

    def outside_tables(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        x0, top = obj.get("x0", 0), obj.get("top", 0)
        for t in tables:
            tx0, ttop, tx1, tbottom = t.bbox
            if tx0 - 1 <= x0 <= tx1 + 1 and ttop - 1 <= top <= tbottom + 1:
                return False
        return True

    if reader is not None:
        # pdfminer 가 글자를 빠뜨린 페이지: 줄 구성과 글자 크기까지 PDFium 기준으로 다시 만든다.
        lines = reader.lines([tuple(t.bbox) for t in tables])
    else:
        text_page = page.filter(outside_tables) if tables else page
        lines = text_page.extract_text_lines(layout=False, strip=True, return_chars=True) or []

    items: list[tuple[float, Block]] = list(_lines_to_blocks(lines, page_no))
    for table in tables:
        markdown = _table_markdown(table, reader)
        if markdown:
            items.append((table.bbox[1], Block(markdown, kind="table", page=page_no)))

    items.sort(key=lambda item: item[0])
    blocks = [b for _, b in items]
    image_count = len(page.images or [])
    if image_count:
        blocks.append(Block(f"[이미지 {image_count}개 포함]", kind="image", page=page_no))
    return blocks


def _table_markdown(table, reader: _PdfiumPage | None) -> str:
    try:
        if reader is None:
            return table_to_markdown(table.extract())
        rows = []
        for row in table.rows:
            rows.append([reader.bounded(*bbox) if bbox else None for bbox in row.cells])
        return table_to_markdown(rows)
    except Exception:
        return ""


def _lines_to_blocks(lines: list[dict], page_no: int) -> list[tuple[float, Block]]:
    """줄 간격과 글자 크기로 문단/제목을 나눈다."""
    if not lines:
        return []
    sizes = []
    for line in lines:
        if "_size" not in line:
            chars = line.get("chars") or []
            line["_size"] = statistics.median([c.get("size", 0) for c in chars]) if chars else 0
        if line["_size"]:
            sizes.append(line["_size"])
    body_size = statistics.median(sizes) if sizes else 0
    heights = [ln["bottom"] - ln["top"] for ln in lines if ln["bottom"] > ln["top"]]
    line_height = statistics.median(heights) if heights else 10

    out: list[tuple[float, Block]] = []
    para: list[str] = []
    para_top = 0.0
    prev_bottom = None

    def flush():
        nonlocal para
        text = clean_text("\n".join(para))
        if text:
            out.append((para_top, Block(text, kind="paragraph", page=page_no)))
        para = []

    for line in lines:
        text = (line.get("text") or "").strip()
        if not text:
            continue
        if body_size > 0 and line["_size"] >= body_size * 1.25 and len(text) <= 60:
            flush()
            level = 1 if line["_size"] >= body_size * 1.6 else 2
            out.append((line["top"], Block(clean_text(text), kind="heading", level=level, page=page_no)))
            prev_bottom = line["bottom"]
            continue
        gap = line["top"] - prev_bottom if prev_bottom is not None else 0
        if para and gap > line_height * 0.9:
            flush()
        if not para:
            para_top = line["top"]
        para.append(text)
        prev_bottom = line["bottom"]
    flush()
    return out


def _pdfium_page_blocks(pdfium_doc, page_no: int) -> list[Block]:
    if pdfium_doc is None:
        return []
    try:
        text = clean_text(pdfium_doc[page_no - 1].get_textpage().get_text_range())
    except Exception:
        return []
    if not text or looks_garbled(text):
        return []
    return [Block(p, kind="paragraph", page=page_no) for p in text.split("\n\n") if p.strip()]


def _meta_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for enc in ("utf-16", "utf-8", "cp949"):
            try:
                return clean_text(value.decode(enc))
            except UnicodeDecodeError:
                continue
        return ""
    return clean_text(str(value))


def _pdf_date(value) -> str:
    """PDF 날짜 'D:20240301120000+09'00'' → '2024-03-01'."""
    text = _meta_str(value)
    if text.startswith("D:"):
        text = text[2:]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return ""
