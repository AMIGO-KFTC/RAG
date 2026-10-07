"""HTML → 블록 변환기. 메일 본문, 웹 페이지, 컨플루언스 본문에서 공통으로 사용한다."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from ..models import Block, ParsedDocument
from ..textutil import clean_text, decode_bytes, table_to_markdown

_DROP_TAGS = {"script", "style", "noscript", "iframe", "svg", "canvas", "template", "head", "button", "select", "option"}
# 컨플루언스 storage 포맷의 매크로 파라미터 등 본문이 아닌 요소
_DROP_PREFIXED = {"ac:parameter", "ri:attachment", "ac:placeholder"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "ul", "ol", "dl", "dt", "dd",
    "blockquote", "figure", "figcaption", "form", "fieldset", "address", "hr", "tbody", "thead", "center",
    "ac:rich-text-body", "ac:layout", "ac:layout-section", "ac:layout-cell", "ac:structured-macro",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def html_to_blocks(html: str, *, drop_chrome: bool = False) -> list[Block]:
    """HTML 문자열을 제목/문단/목록/표 블록으로 변환한다.

    drop_chrome=True 면 nav/header/footer/aside 같은 화면 장식 요소를 버린다(일반 웹 페이지용).
    """
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()
    for name in _DROP_PREFIXED:
        for tag in soup.find_all(name):
            tag.decompose()
    if drop_chrome:
        for tag in soup.find_all(["nav", "header", "footer", "aside"]):
            tag.decompose()
    root = soup.body or soup
    walker = _Walker()
    walker.walk(root)
    walker.flush()
    return walker.blocks


def html_title(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        return clean_text(meta["content"])
    if soup.title and soup.title.string:
        return clean_text(soup.title.string)
    h1 = soup.find("h1")
    return clean_text(h1.get_text(" ")) if h1 else ""


class _Walker:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.buffer: list[str] = []

    def flush(self) -> None:
        text = clean_text("".join(self.buffer))
        self.buffer = []
        if text:
            self.blocks.append(Block(text, kind="paragraph"))

    def walk(self, node: Tag, list_depth: int = 0) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if child.__class__.__name__ in ("Comment", "Doctype", "Declaration", "ProcessingInstruction"):
                    continue
                self.buffer.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = (child.name or "").lower()
            if name in _HEADINGS:
                self.flush()
                text = clean_text(child.get_text(" "))
                if text:
                    self.blocks.append(Block(text, kind="heading", level=_HEADINGS[name]))
            elif name == "table":
                self.flush()
                markdown = _table(child)
                if markdown:
                    self.blocks.append(Block(markdown, kind="table"))
            elif name == "pre" or name == "ac:plain-text-body":
                self.flush()
                text = child.get_text()
                if text.strip():
                    self.blocks.append(Block(text.strip("\n"), kind="code"))
            elif name == "li":
                self.flush()
                inline, nested = _split_nested_lists(child)
                text = clean_text(inline)
                if text:
                    self.blocks.append(Block("  " * list_depth + "- " + text, kind="list"))
                for sub in nested:
                    self.walk(sub, list_depth + 1)
            elif name == "br":
                self.buffer.append("\n")
            elif name in ("ul", "ol"):
                self.flush()
                self.walk(child, list_depth)
                self.flush()
            elif name in _BLOCK_TAGS:
                self.flush()
                self.walk(child, list_depth)
                self.flush()
            elif name == "img":
                alt = clean_text(child.get("alt") or "")
                if alt:
                    self.buffer.append(f"[이미지: {alt}]")
            else:
                self.walk(child, list_depth)


def _split_nested_lists(li: Tag) -> tuple[str, list[Tag]]:
    nested = [c for c in li.find_all(["ul", "ol"], recursive=False)]
    parts = []
    for child in li.children:
        if isinstance(child, Tag) and child.name in ("ul", "ol"):
            continue
        parts.append(child.get_text(" ") if isinstance(child, Tag) else str(child))
    return " ".join(parts), nested


def _table(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:  # 중첩 표의 행은 건너뛴다
            continue
        cells = []
        for cell in tr.find_all(["th", "td"], recursive=False):
            text = cell.get_text(" ")
            cells.append(text)
            try:
                span = int(cell.get("colspan", 1))
            except (TypeError, ValueError):
                span = 1
            cells.extend([""] * (max(span, 1) - 1))
        rows.append(cells)
    return table_to_markdown(rows)


def parse_html_file(path: Path) -> ParsedDocument:
    html = decode_bytes(path.read_bytes())
    doc = ParsedDocument(source_name=path.name, source_type="html")
    doc.metadata["title"] = html_title(html)
    doc.blocks = html_to_blocks(html)
    return doc
