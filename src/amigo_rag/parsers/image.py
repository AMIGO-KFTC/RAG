"""이미지(PNG·JPG) 파서. 스크린샷·사진 속 글자를 읽어 텍스트 블록으로 만든다.

글자 인식(OCR)은 이 모듈이 직접 하지 않고, 쓰는 쪽이 `set_image_reader` 로 등록한 함수가 한다
(BackEnd 가 Claude 가 연결돼 있으면 Claude 비전으로 읽는 함수를 등록한다). 등록이 없으면 읽을 수 없다고 알린다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..models import ParsedDocument, ParseError
from .text import text_to_blocks

IMAGE_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Claude 가 한 장에 받는 한도

ImageReader = Callable[[bytes, str], str]
_reader: ImageReader | None = None


def set_image_reader(reader: ImageReader | None) -> None:
    """이미지 바이트와 형식(예: image/png)을 받아 그 속의 글자를 돌려주는 함수를 등록한다. None 이면 등록을 뺀다."""
    global _reader
    _reader = reader


def parse_image(path: Path) -> ParsedDocument:
    if _reader is None:
        raise ParseError("이미지 속 글자를 읽을 수 없어요(Claude 연결이 필요해요). 글자가 있는 PDF나 문서로 올려 주세요.")
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ParseError(f"이미지가 너무 커요(최대 {MAX_IMAGE_BYTES // 1024 // 1024}MB). 크기를 줄여서 올려 주세요.")
    try:
        text = _reader(data, IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "image/png"))
    except ParseError:
        raise
    except Exception as exc:  # 인식 서비스 오류는 사용자에게 보여 줄 수 있는 문장으로 바꾼다
        raise ParseError(f"이미지 속 글자를 읽지 못했어요: {exc}") from exc
    doc = ParsedDocument(source_name=path.name, source_type="image")
    doc.blocks = text_to_blocks(text.strip())
    if not doc.blocks:
        doc.warnings.append("이미지에서 글자를 찾지 못했어요.")
    return doc
