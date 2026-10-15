"""'자료에 답이 있는가' 판정(relevance / only_relevant) 테스트."""

from amigo_rag.retrieval import content_units


def _ingest_all(kb, sample_dir):
    for path in sorted(sample_dir.iterdir()):
        if path.suffix in {".pdf", ".hwpx", ".docx", ".eml", ".xlsx", ".pptx"}:
            kb.add_file(path)


def test_content_units_drop_endings_and_question_words():
    assert content_units("권한은 어떻게 신청하나요") == {"권한", "신청"}
    assert "회의" in content_units("회의 참석자")  # 한 글자 조사 제거가 '회의'를 '회'로 줄이지 않는다
    assert content_units("ERP 10월") >= {"erp", "10"}
    assert content_units("") == set()


def test_answerable_question_is_flagged_relevant(kb, sample_dir):
    _ingest_all(kb, sample_dir)
    results = kb.search("CMS 권한은 어떻게 신청하나요", top_k=3, only_relevant=True)
    assert results and "IT 권한신청서" in results[0].text
    assert all(r.relevant and r.relevance >= kb.settings.min_relevance for r in results)


def test_unanswerable_question_returns_nothing_when_only_relevant(kb, sample_dir):
    _ingest_all(kb, sample_dir)
    assert kb.search("퇴사자 계정 회수 절차", top_k=5, only_relevant=True) == []
    assert kb.search("출장비 정산은 어떻게 하나요", top_k=5, only_relevant=True) == []


def test_default_search_still_returns_results_but_marks_them(kb, sample_dir):
    """기존 호출은 그대로 결과를 받고, 관련 없는 결과에는 relevant=False 표시가 붙는다."""
    _ingest_all(kb, sample_dir)
    results = kb.search("퇴사자 계정 회수 절차", top_k=3)
    assert len(results) == 3
    assert all(r.relevance is not None for r in results)
    assert not any(r.relevant for r in results)
    assert "relevant" in results[0].to_dict()


def test_min_relevance_threshold_is_configurable(kb, sample_dir):
    _ingest_all(kb, sample_dir)
    kb.settings.min_relevance = 1.1  # 아무것도 통과할 수 없는 기준
    assert kb.search("CMS 권한은 어떻게 신청하나요", only_relevant=True) == []
