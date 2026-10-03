"""파서/청커가 공통으로 쓰는 텍스트 유틸리티."""

from __future__ import annotations

import re
import unicodedata

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff\u200b]")
_SPACES_RE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """제어문자를 없애고 공백을 정리한다. 줄바꿈 구조는 유지한다."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def decode_bytes(data: bytes) -> str:
    """한국어 문서에서 흔한 인코딩(UTF-8, CP949/EUC-KR, UTF-16)을 순서대로 시도한다."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in ("utf-8", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def looks_garbled(text: str) -> bool:
    """PDF 폰트 매핑 실패 등으로 깨진 텍스트인지 대략 판별한다."""
    if not text:
        return False
    if text.count("(cid:") >= 3:
        return True
    bad = sum(1 for ch in text if ch == "\ufffd" or 0xE000 <= ord(ch) <= 0xF8FF)
    return bad / max(len(text), 1) > 0.05


def table_to_markdown(rows: list[list[str | None]]) -> str:
    """2차원 셀 목록을 마크다운 표로 바꾼다. 첫 행을 머리글로 본다."""
    cleaned: list[list[str]] = []
    for row in rows:
        cells = [clean_cell(c) for c in row]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return ""
    width = max(len(r) for r in cleaned)
    # 완전히 빈 열은 제거
    keep = [i for i in range(width) if any(i < len(r) and r[i] for r in cleaned)]
    cleaned = [[r[i] if i < len(r) else "" for i in keep] for r in cleaned]
    if not keep:
        return ""
    if len(cleaned) == 1:
        return " | ".join(c for c in cleaned[0] if c)
    header, *body = cleaned
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def clean_cell(value) -> str:
    if value is None:
        return ""
    text = clean_text(str(value))
    return text.replace("|", "\\|").replace("\n", " ")


def heading_level_from_style(style_name: str | None) -> int:
    """'Heading 2', '제목 2', '개요 3', 'Title' 같은 스타일 이름에서 제목 수준을 추출. 제목이 아니면 0."""
    if not style_name:
        return 0
    name = style_name.strip().lower()
    if name in ("title", "제목"):
        return 1
    match = re.match(r"^(heading|제목|개요|outline)\s*(\d)", name)
    if match:
        return max(1, min(int(match.group(2)), 6))
    return 0
