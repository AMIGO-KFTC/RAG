"""메일 파서: .eml(표준 MIME) 과 .msg(Outlook, 선택 의존성 extract-msg).

메일의 발신자/수신자/일시/제목은 청크 메타데이터로 그대로 전달되어 출처 표기에 쓰인다.
지원 형식의 첨부파일은 재귀적으로 파싱해 attachments 로 붙인다.
"""

from __future__ import annotations

import tempfile
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from ..models import Block, ParsedDocument, ParseError
from ..textutil import clean_text, decode_bytes
from .html import html_to_blocks

MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024


def parse_eml(path: Path, *, _depth: int = 0) -> ParsedDocument:
    with open(path, "rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)
    return _from_email_message(msg, path.name, _depth=_depth)


def _from_email_message(msg: EmailMessage, file_name: str, *, _depth: int) -> ParsedDocument:
    subject = clean_text(str(msg.get("subject", "") or ""))
    senders = getaddresses([str(msg.get("from", "") or "")])
    sender_name, sender_addr = senders[0] if senders else ("", "")
    recipients = [_fmt_addr(n, a) for n, a in getaddresses([str(v) for v in msg.get_all("to", []) or []])]
    cc = [_fmt_addr(n, a) for n, a in getaddresses([str(v) for v in msg.get_all("cc", []) or []])]
    sent_at = ""
    if msg.get("date"):
        try:
            sent_at = parsedate_to_datetime(str(msg["date"])).isoformat()
        except (TypeError, ValueError):
            sent_at = str(msg["date"])

    doc = ParsedDocument(source_name=file_name, source_type="eml")
    doc.metadata.update(
        {
            "title": subject or file_name,
            "subject": subject,
            "sender": _fmt_addr(sender_name, sender_addr),
            "sender_name": clean_text(sender_name) or sender_addr,
            "recipients": ", ".join(r for r in recipients if r),
            "cc": ", ".join(c for c in cc if c),
            "sent_at": sent_at,
        }
    )

    header_lines = [f"제목: {subject}", f"보낸 사람: {doc.metadata['sender']}"]
    if doc.metadata["recipients"]:
        header_lines.append(f"받는 사람: {doc.metadata['recipients']}")
    if doc.metadata["cc"]:
        header_lines.append(f"참조: {doc.metadata['cc']}")
    if sent_at:
        header_lines.append(f"보낸 날짜: {sent_at}")
    doc.blocks.append(Block("\n".join(header_lines), kind="meta"))

    doc.blocks.extend(_body_blocks(msg))

    attachment_names = []
    for part in msg.iter_attachments():
        name = part.get_filename() or "첨부파일"
        attachment_names.append(name)
        if _depth >= 1:
            continue
        child = _parse_attachment(part, name, _depth=_depth + 1, warnings=doc.warnings)
        if child is not None:
            child.metadata.setdefault("parent_name", subject or file_name)
            doc.attachments.append(child)
    if attachment_names:
        doc.metadata["attachments"] = ", ".join(attachment_names)
        doc.blocks.append(Block("첨부파일: " + ", ".join(attachment_names), kind="meta"))
    return doc


def _fmt_addr(name: str, addr: str) -> str:
    name = clean_text(name or "")
    if name and addr:
        return f"{name} <{addr}>"
    return name or addr or ""


def _body_blocks(msg: EmailMessage) -> list[Block]:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return []
    content = _part_text(part)
    if part.get_content_subtype() == "html":
        return html_to_blocks(content)
    text = clean_text(content)
    return [Block(p, kind="paragraph") for p in text.split("\n\n") if p.strip()]


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, AttributeError):
        payload = part.get_payload(decode=True) or b""
        return decode_bytes(payload)


def _parse_attachment(part, name: str, *, _depth: int, warnings: list[str]) -> ParsedDocument | None:
    from . import parse_file, supported_extensions

    suffix = Path(name).suffix.lower()
    if suffix not in supported_extensions():
        return None
    payload = part.get_payload(decode=True) or b""
    if len(payload) > MAX_ATTACHMENT_BYTES:
        warnings.append(f"첨부파일 '{name}' 이(가) 너무 커서 건너뛰었습니다.")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / f"attachment{suffix}"
        tmp_path.write_bytes(payload)
        try:
            child = parse_file(tmp_path, display_name=name, _depth=_depth)
        except ParseError as exc:
            warnings.append(f"첨부파일 '{name}' 을(를) 읽지 못했습니다: {exc}")
            return None
    return child


def parse_msg(path: Path, *, _depth: int = 0) -> ParsedDocument:
    try:
        import extract_msg  # type: ignore
    except ImportError as exc:
        raise ParseError("Outlook .msg 파일을 읽으려면 'pip install amigo-rag[msg]' 가 필요합니다. .eml 로 저장해 올려도 됩니다.") from exc

    message = extract_msg.Message(str(path))
    try:
        doc = ParsedDocument(source_name=path.name, source_type="msg")
        subject = clean_text(message.subject or "")
        doc.metadata.update(
            {
                "title": subject or path.name,
                "subject": subject,
                "sender": clean_text(message.sender or ""),
                "sender_name": clean_text((message.sender or "").split("<")[0]),
                "recipients": clean_text(message.to or ""),
                "cc": clean_text(message.cc or ""),
                "sent_at": message.date.isoformat() if getattr(message, "date", None) else "",
            }
        )
        header = f"제목: {subject}\n보낸 사람: {doc.metadata['sender']}\n받는 사람: {doc.metadata['recipients']}"
        doc.blocks.append(Block(header, kind="meta"))
        body = clean_text(message.body or "")
        doc.blocks.extend(Block(p, kind="paragraph") for p in body.split("\n\n") if p.strip())
        return doc
    finally:
        message.close()
