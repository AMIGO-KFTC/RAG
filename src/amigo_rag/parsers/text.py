"""일반 텍스트, 마크다운, CSV, 소스코드 파서."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from ..models import Block, ParsedDocument
from ..textutil import clean_text, decode_bytes, table_to_markdown

CODE_LANGUAGES = {
    ".py": "python", ".java": "java", ".kt": "kotlin", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".vue": "vue", ".go": "go", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".swift": "swift", ".scala": "scala",
    ".sql": "sql", ".sh": "shell", ".bat": "batch", ".ps1": "powershell", ".xml": "xml", ".yml": "yaml",
    ".yaml": "yaml", ".properties": "properties", ".gradle": "gradle", ".jsp": "jsp", ".css": "css",
    ".scss": "scss", ".ini": "ini", ".toml": "toml", ".json": "json", ".tf": "terraform",
}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".rst"}
CSV_EXTENSIONS = {".csv", ".tsv"}
MAX_TEXT_BYTES = 20 * 1024 * 1024
CSV_ROWS_PER_BLOCK = 40

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def parse_text(path: Path) -> ParsedDocument:
    text = decode_bytes(path.read_bytes()[:MAX_TEXT_BYTES])
    doc = ParsedDocument(source_name=path.name, source_type="markdown" if path.suffix.lower() in (".md", ".markdown") else "text")
    doc.blocks = text_to_blocks(text, markdown=doc.source_type == "markdown")
    return doc


def text_to_blocks(text: str, *, markdown: bool = False) -> list[Block]:
    """빈 줄 기준으로 문단을 나누고, 마크다운 제목(#)은 제목 블록으로 만든다."""
    blocks: list[Block] = []
    paragraph: list[str] = []
    in_code = False

    def flush():
        joined = clean_text("\n".join(paragraph))
        paragraph.clear()
        if joined:
            blocks.append(Block(joined, kind="paragraph"))

    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if markdown and stripped.startswith("```"):
            in_code = not in_code
            paragraph.append(line)
            continue
        if markdown and not in_code:
            match = _MD_HEADING.match(stripped)
            if match:
                flush()
                blocks.append(Block(clean_text(match.group(2)), kind="heading", level=len(match.group(1))))
                continue
        if not stripped and not in_code:
            flush()
            continue
        paragraph.append(line)
    flush()
    return blocks


def parse_csv(path: Path) -> ParsedDocument:
    text = decode_bytes(path.read_bytes()[:MAX_TEXT_BYTES])
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in row)]
    doc = ParsedDocument(source_name=path.name, source_type="csv")
    if not rows:
        return doc
    header, body = rows[0], rows[1:]
    if not body:
        doc.blocks.append(Block(table_to_markdown([header]), kind="table"))
    for start in range(0, len(body), CSV_ROWS_PER_BLOCK):
        doc.blocks.append(Block(table_to_markdown([header] + body[start : start + CSV_ROWS_PER_BLOCK]), kind="table"))
    return doc


def parse_code(path: Path) -> ParsedDocument:
    text = decode_bytes(path.read_bytes()[:MAX_TEXT_BYTES]).replace("\r\n", "\n")
    language = CODE_LANGUAGES.get(path.suffix.lower(), "text")
    doc = ParsedDocument(source_name=path.name, source_type="code")
    doc.metadata["language"] = language
    # 코드는 빈 줄로 구분된 덩어리(함수/클래스 단위에 가까움)로 나눈다. 공백·들여쓰기는 보존.
    chunk: list[str] = []
    for line in text.split("\n"):
        if not line.strip() and chunk and sum(len(c) for c in chunk) > 200:
            blocks_text = "\n".join(chunk).strip("\n")
            if blocks_text.strip():
                doc.blocks.append(Block(blocks_text, kind="code", meta={"language": language}))
            chunk = []
        else:
            chunk.append(line)
    rest = "\n".join(chunk).strip("\n")
    if rest.strip():
        doc.blocks.append(Block(rest, kind="code", meta={"language": language}))
    return doc
