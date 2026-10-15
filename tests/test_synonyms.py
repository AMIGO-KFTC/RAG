"""동의어 사전 테스트: 단어가 달라도 뜻이 같으면 관련 있음으로 본다."""

import json
import uuid

from amigo_rag import KnowledgeBase, RAGSettings
from amigo_rag.synonyms import SynonymDict


def test_groups_merge_and_normalize():
    d = SynonymDict([["대금", "비용"], ["비용", "요금"], ["하나"]])
    assert d.groups == [["대금", "비용", "요금"]]


def test_expand_adds_missing_synonyms_only():
    d = SynonymDict([["대금", "비용", "요금"]])
    assert set(d.expand("월 대금 지급").split()) == {"비용", "요금"}
    assert d.expand("관련 없는 문장") == ""


def test_load_save_roundtrip_and_edit(tmp_path):
    path = tmp_path / "syn.json"
    d = SynonymDict.load(path)  # 파일이 없어도 빈 사전
    assert not d
    d.path = path
    d.add_group(["인계자", "전임자"])
    d.save()
    assert json.loads(path.read_text(encoding="utf-8"))["groups"] == [["인계자", "전임자"]]
    d2 = SynonymDict.load(path)
    d2.remove_term("전임자")
    assert not d2


def test_bad_file_raises_on_load_but_refresh_keeps_old(tmp_path):
    path = tmp_path / "syn.json"
    path.write_text('{"groups": [["a1", "b1"]]}', encoding="utf-8")
    d = SynonymDict.load(path)
    path.write_text("not json", encoding="utf-8")
    import os; os.utime(path, (1, 1))
    d.refresh()
    assert d.groups == [["a1", "b1"]]


def _kb(tmp_path, syn_path):
    settings = RAGSettings.in_memory()
    settings.synonyms_path = syn_path
    return KnowledgeBase(f"syn-{uuid.uuid4().hex[:8]}", settings=settings)


def test_synonym_makes_different_word_relevant(tmp_path, sample_dir):
    kb0 = _kb(tmp_path, None)
    for p in sorted(sample_dir.iterdir()):
        if p.suffix == ".eml":
            kb0.add_file(p)
    question = "유지보수 업체에 비용 지급은 누구에게 요청하나요"
    before = kb0.search(question, top_k=3)
    syn = tmp_path / "syn.json"
    syn.write_text(json.dumps({"groups": [["대금", "비용"]]}, ensure_ascii=False), encoding="utf-8")
    kb1 = _kb(tmp_path, syn)
    for p in sorted(sample_dir.iterdir()):
        if p.suffix == ".eml":
            kb1.add_file(p)
    after = kb1.search(question, top_k=3)
    assert after and after[0].relevance > before[0].relevance
    # 사전을 고치면 다음 검색부터 바로 반영된다.
    kb1.synonyms.set_groups([])
    assert kb1.search(question, top_k=3)[0].relevance == before[0].relevance
