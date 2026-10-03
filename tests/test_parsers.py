import struct
import zipfile
import zlib
from email.message import EmailMessage
from pathlib import Path

import pytest

from amigo_rag import ParseError, parse_file
from amigo_rag.parsers.html import html_to_blocks
from amigo_rag.parsers.hwp import decode_para_text, iter_paragraph_texts
from amigo_rag.textutil import decode_bytes, table_to_markdown


def test_pdf_text_tables_headings_and_pages(sample_dir: Path):
    doc = parse_file(sample_dir / "업무정의서_웹서비스팀.pdf")
    assert doc.source_type == "pdf"
    assert doc.metadata["page_count"] == 2
    assert doc.metadata["author"] == "김민수"
    headings = [b.text for b in doc.blocks if b.kind == "heading"]
    assert "2. 주요 업무" in headings
    table = next(b for b in doc.blocks if b.kind == "table")
    assert table.text.startswith("| 업무명 | 주요 내용 | 비중 |")
    # pdfminer 가 빠뜨리는 가운뎃점(·)도 PDFium 보정으로 살아 있어야 한다
    assert "공지사항·보도자료" in table.text
    assert "분기별(1·4·7·10월): 웹 접근성 자체 점검 및 개선 요청" in doc.text
    page2 = [b for b in doc.blocks if b.page == 2]
    assert any("박지훈 차장" in b.text for b in page2)


def test_hwpx_sections_styles_and_tables(sample_dir: Path):
    doc = parse_file(sample_dir / "홈페이지_운영매뉴얼.hwpx")
    assert doc.metadata["title"] == "홈페이지 운영 매뉴얼"
    assert doc.metadata["author"] == "김민수"
    heading_levels = {b.text: b.level for b in doc.blocks if b.kind == "heading"}
    assert heading_levels["홈페이지 운영 매뉴얼"] == 1
    assert heading_levels["2. 장애 대응 절차"] == 2
    table = next(b for b in doc.blocks if b.kind == "table")
    assert "| Google Analytics | 방문 통계 분석 | 편집자 |" in table.text
    assert "30분 이내에 IT지원팀" in doc.text


def test_hwpx_falls_back_to_preview(tmp_path: Path):
    path = tmp_path / "broken.hwpx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Contents/section0.xml", "<not-xml")
        zf.writestr("Preview/PrvText.txt", "미리보기 본문입니다")
    doc = parse_file(path)
    assert "미리보기 본문입니다" in doc.text
    assert doc.warnings


def test_hwp_record_decoding():
    text = "홈페이지 담당\t업무"
    # 탭(9)은 8 wchar 인라인 제어문자, 13 은 문단 끝
    units = [ord(c) for c in "홈페이지 담당"] + [9] + [0] * 7 + [ord(c) for c in "업무"] + [13]
    payload = struct.pack(f"<{len(units)}H", *units)
    assert decode_para_text(payload) == text

    header = (67 & 0x3FF) | (0 << 10) | (len(payload) << 20)
    record = struct.pack("<I", header) + payload
    assert list(iter_paragraph_texts(record)) == [text]
    # 압축된 스트림도 같은 결과
    compressed = zlib.compress(record)[2:-4]
    assert list(iter_paragraph_texts(zlib.decompress(compressed, -15))) == [text]


def test_hwp_rejects_non_ole(tmp_path: Path):
    path = tmp_path / "fake.hwp"
    path.write_bytes(b"not an ole file")
    with pytest.raises(ParseError):
        parse_file(path)


def test_docx_tables_lists_and_headings(sample_dir: Path):
    doc = parse_file(sample_dir / "주간회의록_2026-09-22.docx")
    assert any(b.kind == "heading" and b.text == "1. 진행 중인 과제" for b in doc.blocks)
    table = next(b for b in doc.blocks if b.kind == "table")
    assert "| 개인정보처리방침 개정 반영 | 법무팀 검토 중 | 10/15까지 게시 필요 |" in table.text
    lists = [b.text for b in doc.blocks if b.kind == "list"]
    assert "- 김민수: RFP 최종안 10/2까지 공유" in lists


def test_eml_metadata_body_and_attachment(sample_dir: Path):
    doc = parse_file(sample_dir / "재무팀_유지보수대금_요청.eml")
    assert doc.metadata["subject"] == "[재무팀] 9월 홈페이지 유지보수 대금 지급 관련 요청"
    assert doc.metadata["sender_name"] == "박지훈"
    assert doc.metadata["sent_at"].startswith("2026-09-30")
    assert "10월 7일까지" in doc.text
    assert len(doc.attachments) == 1
    attachment = doc.attachments[0]
    assert attachment.source_name == "검수_체크리스트.txt"
    assert "장애 처리 건수" in attachment.text


def test_eml_with_legacy_korean_charset(tmp_path: Path):
    msg = EmailMessage()
    msg["Subject"] = "테스트"
    msg["From"] = "홍길동 <hong@example.com>"
    msg.set_content("본문 내용입니다".encode("cp949"), maintype="text", subtype="plain", cte="base64")
    msg.replace_header("Content-Type", 'text/plain; charset="ks_c_5601-1987"')
    path = tmp_path / "legacy.eml"
    path.write_bytes(bytes(msg))
    doc = parse_file(path)
    assert "본문 내용입니다" in doc.text


def test_xlsx_sheets(sample_dir: Path):
    doc = parse_file(sample_dir / "시스템_계정목록.xlsx")
    tables = [b for b in doc.blocks if b.kind == "table"]
    assert {b.meta["sheet"] for b in tables} == {"계정 목록", "연간 일정"}
    assert any("SSL 인증서 갱신" in b.text for b in tables)


def test_pptx_slides_and_notes(sample_dir: Path):
    doc = parse_file(sample_dir / "웹접근성_개선계획.pptx")
    assert doc.metadata["page_count"] == 3
    slide3 = [b.text for b in doc.blocks if b.page == 3]
    assert "추진 일정" in slide3
    assert any(t.startswith("발표자 노트:") for t in slide3)


class _CP949Info(zipfile.ZipInfo):
    """한국어 윈도우 압축 프로그램처럼 파일명을 CP949 로 기록(UTF-8 플래그 없음)."""

    def _encodeFilenameFlags(self):
        return self.filename.encode("cp949"), self.flag_bits


def test_zip_with_code_and_cp949_names(tmp_path: Path):
    path = tmp_path / "src.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("app/main.py", "def hello():\n    return '안녕'\n")
        zf.writestr(_CP949Info("문서/설명.md"), "# 설명\n\n배치 작업은 매일 02시에 실행된다.".encode("utf-8"))
        zf.writestr("node_modules/x.js", "ignored")
        zf.writestr("image.png", b"\x89PNG")
    doc = parse_file(path)
    names = sorted(a.source_name for a in doc.attachments)
    assert names == ["app/main.py", "문서/설명.md"]
    code = next(a for a in doc.attachments if a.source_name == "app/main.py")
    assert code.metadata["language"] == "python"
    assert code.blocks[0].kind == "code"


def test_html_confluence_storage_macros():
    html = """
    <h1>배치 운영</h1>
    <p>매일 <strong>02시</strong> 배치가 실행된다.<br/>실패 시 재실행한다.</p>
    <ac:structured-macro ac:name="info"><ac:parameter ac:name="title">무시할 파라미터</ac:parameter>
      <ac:rich-text-body><p>장애 시 운영팀에 연락</p></ac:rich-text-body></ac:structured-macro>
    <ul><li>항목 A<ul><li>세부 B</li></ul></li></ul>
    <table><tr><th>시스템</th><th>담당</th></tr><tr><td>ERP</td><td>재무팀</td></tr></table>
    <script>alert(1)</script>
    """
    blocks = html_to_blocks(html)
    kinds = [(b.kind, b.text) for b in blocks]
    assert ("heading", "배치 운영") in kinds
    assert any(b.kind == "paragraph" and "02시" in b.text and "\n" in b.text for b in blocks)
    texts = " ".join(b.text for b in blocks)
    assert "무시할 파라미터" not in texts and "alert" not in texts
    assert "장애 시 운영팀에 연락" in texts
    assert ("list", "- 항목 A") in kinds and ("list", "  - 세부 B") in kinds
    assert any(b.kind == "table" and "| ERP | 재무팀 |" in b.text for b in blocks)


def test_unsupported_and_missing_files(tmp_path: Path):
    path = tmp_path / "a.exe"
    path.write_bytes(b"MZ")
    with pytest.raises(ParseError, match="지원하지 않는"):
        parse_file(path)
    with pytest.raises(ParseError):
        parse_file(tmp_path / "missing.pdf")


def test_decode_bytes_and_markdown_table():
    assert decode_bytes("한글".encode("cp949")) == "한글"
    assert decode_bytes("﻿한글".encode("utf-8")) == "한글"
    md = table_to_markdown([["a", None, "b|c"], ["1", None, "2\n3"]])
    assert md == "| a | b\\|c |\n|---|---|\n| 1 | 2 3 |"
