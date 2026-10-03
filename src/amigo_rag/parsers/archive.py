"""ZIP 압축파일 파서. 소스코드 묶음이나 자료 모음을 한 번에 올릴 때 사용한다.

압축 폭탄·경로 조작을 막기 위해 항목 수/용량을 제한하고, 압축 안의 경로를 그대로 쓰지 않고
임시 디렉터리에 고정된 이름으로 풀어서 파싱한다.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from ..models import Block, ParsedDocument, ParseError

MAX_ENTRIES = 500
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ENTRY_BYTES = 30 * 1024 * 1024
_SKIP_DIRS = {"__MACOSX", ".git", "node_modules", ".idea", ".vscode", "build", "dist", "target", "__pycache__", ".venv", "venv"}


def parse_zip(path: Path, *, _depth: int = 0) -> ParsedDocument:
    from . import parse_file, supported_extensions

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ParseError("ZIP 파일이 손상되었습니다.") from exc

    doc = ParsedDocument(source_name=path.name, source_type="zip")
    supported = supported_extensions() - {".zip"}
    with zf, tempfile.TemporaryDirectory() as tmp:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            doc.warnings.append(f"압축 안의 파일이 너무 많아 앞 {MAX_ENTRIES}개만 확인했습니다.")
            infos = infos[:MAX_ENTRIES]
        total = 0
        listing = []
        for index, info in enumerate(infos):
            entry_name = _decode_name(info)
            parts = PurePosixPath(entry_name).parts
            if any(p in _SKIP_DIRS or p.startswith(".") for p in parts):
                continue
            suffix = PurePosixPath(entry_name).suffix.lower()
            if suffix not in supported:
                continue
            if info.file_size > MAX_ENTRY_BYTES:
                doc.warnings.append(f"'{entry_name}' 은(는) 너무 커서 건너뛰었습니다.")
                continue
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                doc.warnings.append("압축 해제 용량 제한(200MB)에 도달해 나머지 파일은 건너뛰었습니다.")
                break
            target = Path(tmp) / f"entry_{index}{suffix}"
            with zf.open(info) as src, open(target, "wb") as dst:
                remaining = MAX_ENTRY_BYTES + 1
                while remaining > 0:
                    data = src.read(min(1024 * 1024, remaining))
                    if not data:
                        break
                    dst.write(data)
                    remaining -= len(data)
            try:
                child = parse_file(target, display_name=entry_name, _depth=_depth + 1)
            except ParseError as exc:
                doc.warnings.append(f"'{entry_name}' 을(를) 읽지 못했습니다: {exc}")
                continue
            child.metadata.setdefault("parent_name", path.name)
            child.metadata.setdefault("path_in_archive", entry_name)
            doc.attachments.append(child)
            listing.append(entry_name)
        doc.metadata["file_count"] = len(listing)
        if listing:
            preview = "\n".join(f"- {name}" for name in listing[:200])
            doc.blocks.append(Block(f"압축파일 '{path.name}' 구성:\n{preview}", kind="meta"))
    return doc


def _decode_name(info: zipfile.ZipInfo) -> str:
    """한국어 윈도우에서 만든 ZIP 은 파일명이 CP949 인 경우가 많다."""
    name = info.filename
    if info.flag_bits & 0x800:  # UTF-8 플래그
        return name
    try:
        return name.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
