"""파일 확장자별 파서 등록부.

새 형식을 지원하려면 `(Path) -> ParsedDocument` 함수를 만들고 PARSERS 에 확장자를 추가하면 된다.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from ..models import ParsedDocument, ParseError
from .archive import parse_zip
from .html import html_to_blocks, parse_html_file
from .hwp import parse_hwp
from .hwpx import parse_hwpx
from .mail import parse_eml, parse_msg
from .office import parse_docx, parse_pptx, parse_xlsx
from .pdf import parse_pdf
from .text import CODE_LANGUAGES, CSV_EXTENSIONS, TEXT_EXTENSIONS, parse_code, parse_csv, parse_text

Parser = Callable[..., ParsedDocument]

PARSERS: dict[str, Parser] = {
    ".pdf": parse_pdf,
    ".hwpx": parse_hwpx,
    ".hwp": parse_hwp,
    ".docx": parse_docx,
    ".pptx": parse_pptx,
    ".xlsx": parse_xlsx,
    ".xlsm": parse_xlsx,
    ".eml": parse_eml,
    ".msg": parse_msg,
    ".html": parse_html_file,
    ".htm": parse_html_file,
    ".zip": parse_zip,
}
PARSERS.update({ext: parse_text for ext in TEXT_EXTENSIONS})
PARSERS.update({ext: parse_csv for ext in CSV_EXTENSIONS})
PARSERS.update({ext: parse_code for ext in CODE_LANGUAGES})

_NESTED = {parse_zip, parse_eml, parse_msg}

FORMAT_LABELS = {
    "pdf": "PDF", "hwpx": "한글(HWPX)", "hwp": "한글(HWP)", "docx": "Word", "pptx": "PowerPoint",
    "xlsx": "Excel", "eml": "메일", "msg": "메일", "html": "HTML", "zip": "압축파일", "text": "텍스트",
    "markdown": "마크다운", "csv": "CSV", "code": "소스코드",
}


def supported_extensions() -> set[str]:
    return set(PARSERS)


def parse_file(path: str | Path, *, display_name: str | None = None, _depth: int = 0) -> ParsedDocument:
    """파일 하나를 ParsedDocument 로 변환한다. 지원하지 않는 형식이면 ParseError."""
    path = Path(path)
    if not path.is_file():
        raise ParseError(f"파일을 찾을 수 없습니다: {path}")
    name = display_name or path.name
    suffix = Path(name).suffix.lower() or path.suffix.lower()
    parser = PARSERS.get(suffix) or PARSERS.get(path.suffix.lower())
    if parser is None:
        raise ParseError(f"지원하지 않는 파일 형식입니다: {suffix or '(확장자 없음)'}")
    if _depth > 2:
        raise ParseError("중첩된 압축/첨부가 너무 깊습니다.")
    doc = parser(path, _depth=_depth) if parser in _NESTED else parser(path)
    doc.source_name = name
    return doc


__all__ = ["PARSERS", "FORMAT_LABELS", "parse_file", "supported_extensions", "html_to_blocks", "ParseError"]
