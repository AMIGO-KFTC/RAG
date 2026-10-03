from amigo_rag import Block, ParsedDocument, parse_file
from amigo_rag.chunking import chunk_document


def test_sections_pages_and_metadata(sample_dir):
    doc = parse_file(sample_dir / "업무정의서_웹서비스팀.pdf")
    chunks = chunk_document(doc, source_id="src1")
    assert [c.id for c in chunks] == [f"src1:{i:04d}" for i in range(len(chunks))]
    table_chunk = next(c for c in chunks if "table" in c.metadata["kinds"])
    assert table_chunk.metadata["section"] == "웹서비스팀 업무정의서 > 2. 주요 업무"
    assert table_chunk.metadata["page"] == 1
    assert table_chunk.text.startswith("## 2. 주요 업무")
    for c in chunks:
        assert all(isinstance(v, (str, int, float, bool)) for v in c.metadata.values())
        assert c.metadata["source_id"] == "src1"


def test_large_table_repeats_header():
    rows = ["| 시스템 | 담당 |", "|---|---|"] + [f"| 시스템{i:03d} | 담당자{i:03d} 연락처 010-0000-{i:04d} |" for i in range(80)]
    doc = ParsedDocument("t.xlsx", "xlsx", blocks=[Block("\n".join(rows), kind="table")])
    chunks = chunk_document(doc, source_id="t", chunk_size=400)
    assert len(chunks) > 3
    assert all(c.text.startswith("| 시스템 | 담당 |\n|---|---|") for c in chunks)
    joined = "\n".join(c.text for c in chunks)
    assert "시스템079" in joined


def test_long_paragraph_split_with_overlap():
    sentences = [f"{i}번째 문장은 업무 절차에 대한 설명이다." for i in range(60)]
    doc = ParsedDocument("long.txt", "text", blocks=[Block(" ".join(sentences))])
    chunks = chunk_document(doc, source_id="l", chunk_size=300, chunk_overlap=60)
    assert len(chunks) >= 6
    assert all(len(c.text) <= 300 for c in chunks)
    # 앞 청크의 마지막 문장이 다음 청크 앞에 겹쳐 들어간다
    first_tail = chunks[0].text.split(". ")[-1]
    assert first_tail.strip(".") in chunks[1].text


def test_paragraph_packing_and_heading_boundaries():
    blocks = [Block("개요", kind="heading", level=1)]
    blocks += [Block(f"문단 {i} 내용입니다.") for i in range(3)]
    blocks += [Block("세부", kind="heading", level=2), Block("세부 내용입니다.")]
    doc = ParsedDocument("d.docx", "docx", blocks=blocks)
    chunks = chunk_document(doc, source_id="d")
    assert len(chunks) == 2
    assert chunks[0].metadata["section"] == "개요"
    assert chunks[1].metadata["section"] == "개요 > 세부"


def test_code_blocks_keep_line_breaks():
    code = "\n".join(f"    line_{i} = {i}" for i in range(200))
    doc = ParsedDocument("a.py", "code", blocks=[Block(code, kind="code")])
    chunks = chunk_document(doc, source_id="c", chunk_size=500)
    assert len(chunks) > 3
    assert all("\n    line_" in c.text for c in chunks)


def test_email_attachment_inherits_sender(sample_dir):
    doc = parse_file(sample_dir / "재무팀_유지보수대금_요청.eml")
    chunks = chunk_document(doc, source_id="m")
    attachment_chunk = next(c for c in chunks if c.metadata["file_name"] == "검수_체크리스트.txt")
    assert attachment_chunk.metadata["source_type"] == "eml"
    assert attachment_chunk.metadata["file_type"] == "text"
    assert attachment_chunk.metadata["sender_name"] == "박지훈"
    assert attachment_chunk.metadata["parent_name"].startswith("[재무팀]")
