from amigo_rag import SearchResult, format_citation


def _load_samples(kb, sample_dir):
    results = {}
    for path in sorted(sample_dir.iterdir()):
        results[path.name] = kb.add_file(path)
    return results


def test_ingest_and_search_with_citations(kb, sample_dir):
    results = _load_samples(kb, sample_dir)
    assert all(r.chunk_count > 0 for r in results.values())

    hits = kb.search("CMS 권한 신청 방법", top_k=3)
    assert hits and hits[0].metadata["file_name"] == "홈페이지_운영매뉴얼.hwpx"
    assert isinstance(hits[0], SearchResult)
    assert 0 < hits[0].score <= 1

    hits = kb.search("재무팀 박지훈 요청 기한", top_k=3)
    assert any(h.metadata["source_type"] == "eml" for h in hits)
    mail = next(h for h in hits if h.metadata["source_type"] == "eml")
    assert mail.citation.startswith("메일 '[재무팀] 9월 홈페이지 유지보수 대금 지급 관련 요청'")
    assert "박지훈" in mail.citation

    hits = kb.search("웹 접근성 품질인증 갱신", top_k=5)
    names = {h.metadata["file_name"] for h in hits}
    assert "웹접근성_개선계획.pptx" in names


def test_filters_per_source_limit_and_to_dict(kb, sample_dir):
    _load_samples(kb, sample_dir)
    hits = kb.search("유지보수", top_k=10, source_types=["pdf"])
    assert hits and all(h.metadata["source_type"] == "pdf" for h in hits)
    hits = kb.search("유지보수 업체", top_k=10, per_source_limit=1)
    sources = [h.source_id for h in hits]
    assert len(sources) == len(set(sources))
    data = hits[0].to_dict()
    assert data["citation"] == hits[0].citation and "metadata" in data


def test_add_text_is_searchable_and_cited(kb):
    kb.add_text(
        "유지보수 업체 담당자는 한빛웹솔루션 정우성 실장(010-1234-5678)이다.",
        source_name="인계자 답변",
        source_type="chat",
    )
    hits = kb.search("유지보수 업체 담당자 연락처")
    assert hits and "정우성" in hits[0].text
    assert hits[0].citation.startswith("인계자 답변")


def test_reingest_replaces_and_delete_source(kb, sample_dir):
    first = kb.add_file(sample_dir / "주간회의록_2026-09-22.docx", source_id="docx1")
    again = kb.add_file(sample_dir / "주간회의록_2026-09-22.docx", source_id="docx1")
    assert kb.stats()["chunk_count"] == again.chunk_count == first.chunk_count
    sources = kb.list_sources()
    assert [s.source_id for s in sources] == ["docx1"]
    assert sources[0].chunk_count == first.chunk_count
    assert kb.delete_source("docx1") == first.chunk_count
    assert kb.search("리뉴얼") == []


def test_get_chunks_in_order(kb, sample_dir):
    kb.add_file(sample_dir / "업무정의서_웹서비스팀.pdf", source_id="pdf1")
    chunks = kb.get_chunks("pdf1")
    assert [c.metadata["chunk_index"] for c in chunks] == sorted(c.metadata["chunk_index"] for c in chunks)


def test_empty_queries_and_unknown_kb(kb):
    assert kb.search("") == []
    assert kb.search("아무거나") == []


def test_format_citation_variants():
    assert format_citation({"file_name": "a.pdf", "file_type": "pdf", "page": 3}) == "a.pdf p.3"
    assert format_citation({"file_name": "a.pdf", "file_type": "pdf", "page": 3, "page_end": 4}) == "a.pdf p.3-4"
    assert format_citation({"file_name": "b.pptx", "file_type": "pptx", "page": 2}) == "b.pptx 슬라이드 2"
    assert format_citation({"file_name": "c.xlsx", "file_type": "xlsx", "page": 1, "sheet": "목록"}) == "c.xlsx [목록]"
    assert format_citation({"source_name": "운영 가이드", "file_type": "confluence"}) == "컨플루언스: 운영 가이드"
