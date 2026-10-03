"""구버전 HWP(한글 5.0 바이너리) 파서.

HWP 5.0 은 OLE 복합 파일이다. 본문은 BodyText/Section0, Section1 … 스트림에 레코드 형태로 들어 있고,
FileHeader 의 플래그에 따라 raw-deflate 로 압축되어 있다. 문단 텍스트는 HWPTAG_PARA_TEXT(67) 레코드에
UTF-16LE 로 저장되며, 32 미만 코드는 제어문자(일부는 16바이트 확장 제어)다.
표 구조는 복원하지 않고 셀 텍스트를 문단으로 이어 붙인다.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from ..models import Block, ParsedDocument, ParseError
from ..textutil import clean_text

HWPTAG_BEGIN = 0x10
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51
# 1 wchar 크기의 제어문자. 나머지(1~9, 11~12, 14~23)는 8 wchar(16바이트) 확장/인라인 제어.
_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}
MAX_STREAM_BYTES = 128 * 1024 * 1024


def parse_hwp(path: Path) -> ParsedDocument:
    import olefile

    if not olefile.isOleFile(str(path)):
        raise ParseError("HWP 파일 형식이 아닙니다. (HWPX 문서라면 확장자를 .hwpx 로 바꿔 주세요)")

    doc = ParsedDocument(source_name=path.name, source_type="hwp")
    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists("FileHeader"):
            raise ParseError("HWP FileHeader 가 없습니다. 손상된 파일일 수 있습니다.")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise ParseError("HWP 5.0 문서가 아닙니다. (HWP 3.0 이하 문서는 지원하지 않습니다)")
        flags = struct.unpack_from("<I", header, 36)[0] if len(header) >= 40 else 0
        compressed = bool(flags & 0x1)
        if flags & 0x2:
            raise ParseError("암호가 설정된 HWP 문서는 읽을 수 없습니다. 암호를 해제한 뒤 다시 올려 주세요.")
        distribution = bool(flags & 0x4)

        summary = _summary_info(ole)
        doc.metadata.update(summary)

        sections = sorted(
            (entry for entry in ole.listdir() if len(entry) == 2 and entry[0] == "BodyText"),
            key=lambda e: _section_index(e[1]),
        )
        for entry in sections:
            data = ole.openstream(entry).read(MAX_STREAM_BYTES)
            if compressed:
                try:
                    data = zlib.decompress(data, -15)
                except zlib.error:
                    doc.warnings.append(f"{'/'.join(entry)} 압축 해제에 실패했습니다.")
                    continue
            for text in iter_paragraph_texts(data):
                text = clean_text(text)
                if text:
                    doc.blocks.append(Block(text, kind="paragraph"))

        if not doc.blocks and ole.exists("PrvText"):
            preview = clean_text(ole.openstream("PrvText").read().decode("utf-16-le", errors="replace"))
            if preview:
                reason = "배포용 문서라 본문이 암호화되어" if distribution else "본문을 해석하지 못해"
                doc.warnings.append(f"{reason} 미리보기 텍스트(앞부분 일부)만 추출했습니다.")
                doc.blocks = [Block(p, kind="paragraph") for p in preview.split("\n") if p.strip()]
    return doc


def _section_index(name: str) -> int:
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


def iter_records(data: bytes):
    """(tag_id, level, payload) 레코드를 순서대로 반환."""
    pos, size_total = 0, len(data)
    while pos + 4 <= size_total:
        (header,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag_id = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > size_total:
                break
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        payload = data[pos : pos + size]
        pos += size
        yield tag_id, level, payload


def iter_paragraph_texts(data: bytes):
    for tag_id, _level, payload in iter_records(data):
        if tag_id == HWPTAG_PARA_TEXT:
            yield decode_para_text(payload)


def decode_para_text(payload: bytes) -> str:
    """PARA_TEXT 레코드의 UTF-16LE 텍스트에서 제어문자를 걸러낸다."""
    count = len(payload) // 2
    units = struct.unpack_from(f"<{count}H", payload) if count else ()
    out: list[str] = []
    run = bytearray()

    def flush_run():
        if run:
            out.append(run.decode("utf-16-le", errors="replace"))
            run.clear()

    i = 0
    while i < count:
        code = units[i]
        if code >= 32:
            run += payload[i * 2 : i * 2 + 2]
            i += 1
            continue
        flush_run()
        if code in _CHAR_CONTROLS:
            if code == 10:
                out.append("\n")
            elif code in (24,):
                out.append("-")
            elif code in (30, 31):
                out.append(" ")
            i += 1
        else:
            if code == 9:
                out.append("\t")
            i += 8
    flush_run()
    return "".join(out)


def _summary_info(ole) -> dict:
    """\\x05HwpSummaryInformation 스트림에서 제목/작성자를 읽는다(실패해도 무시)."""
    meta: dict = {}
    try:
        name = "\x05HwpSummaryInformation"
        if ole.exists(name):
            props = ole.getproperties(name, convert_time=True)
            title = props.get(2)
            author = props.get(4)
            if isinstance(title, (str, bytes)):
                meta["title"] = clean_text(title.decode("utf-16-le", "ignore") if isinstance(title, bytes) else title)
            if isinstance(author, (str, bytes)):
                meta["author"] = clean_text(author.decode("utf-16-le", "ignore") if isinstance(author, bytes) else author)
    except Exception:
        pass
    return {k: v for k, v in meta.items() if v}
