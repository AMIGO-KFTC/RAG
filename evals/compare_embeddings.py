"""임베딩 비교: 같은 질문 세트로 여러 임베딩의 순위 정확도와 '자료에 없음' 판정을 나란히 잰다.

    python evals/compare_embeddings.py hash st:jhgan/ko-sroberta-multitask st:BAAI/bge-m3

st: 모델은 `pip install -e ".[st]"` 와 모델 다운로드(인터넷 또는 사내 미러)가 필요하다. 모델마다 처음 한 번 내려받는다.

읽는 법
- 순위 정확도: 정답이 들어 있는 조각을 1위(hit@1)/상위 3위(hit@3)에 찾는 비율. 세 질문 세트를 합친 값(pooled)을 함께 보인다.
- 없음 판정(단어 겹침): 검색 결과의 relevant(AMIGO_RAG_MIN_RELEVANCE 기준) 로 판정. 임베딩과 무관한 방식이지만,
  임베딩에 따라 상위 5개 후보가 달라 값이 조금 달라진다.
- 없음 판정(벡터 유사도): 벡터 유사도가 기준값 이상이면 '있음'. 기준값은 dev+heldout 에서 정하고 test 에는 적용만 해서
  처음 보는 질문에서의 성능을 본다.
test 세트는 개선 작업이 끝난 뒤 확인용으로만 읽는다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import retrieval_eval as E  # noqa: E402


def collect(spec: str) -> dict[str, list[dict]]:
    kb = E.build_kb(None if spec == "hash" else spec)
    records: dict[str, list[dict]] = {}
    for name, (yes, no) in E.SETS.items():
        rows = []
        for question, expected in yes:
            results = kb.search(question, top_k=5)
            rows.append({"yes": True, "hits": [(bool(re.search(expected, r.text)), r.vector_score, r.relevant) for r in results]})
        for question in no:
            results = kb.search(question, top_k=5)
            rows.append({"yes": False, "hits": [(False, r.vector_score, r.relevant) for r in results]})
        records[name] = rows
    return records


def ranking(rows: list[dict]) -> tuple[int, int, int]:
    yes = [r for r in rows if r["yes"]]
    ranks = [next((i + 1 for i, h in enumerate(r["hits"]) if h[0]), None) for r in yes]
    return sum(k == 1 for k in ranks), sum(k is not None and k <= 3 for k in ranks), len(yes)


def judge(rows: list[dict], ok) -> tuple[float, float]:
    """(있는 질문 유지율, 없는 질문 걸러낸 비율). ok(hit)->bool 은 결과 하나를 '관련 있음'으로 볼지."""
    yes = [r for r in rows if r["yes"]]
    no = [r for r in rows if not r["yes"]]
    found = sum(any(h[0] and ok(h) for h in r["hits"]) for r in yes) / len(yes)
    rejected = sum(not any(ok(h) for h in r["hits"]) for r in no) / len(no)
    return found, rejected


def main() -> None:
    specs = sys.argv[1:] or ["hash"]
    for spec in specs:
        rec = collect(spec)
        print(f"\n=== {spec} ===")
        pooled = [0, 0, 0]
        print("순위 정확도          hit@1    hit@3")
        for name, rows in rec.items():
            h1, h3, n = ranking(rows)
            pooled = [pooled[0] + h1, pooled[1] + h3, pooled[2] + n]
            print(f"  {name:8}        {h1 / n:6.1%}  {h3 / n:6.1%}   ({n}문항)")
        print(f"  {'pooled':8}        {pooled[0] / pooled[2]:6.1%}  {pooled[1] / pooled[2]:6.1%}   ({pooled[2]}문항)")

        print("없음 판정 (있는 질문 유지 / 없는 질문 걸러냄)")
        word = lambda h: bool(h[2])  # noqa: E731
        for name, rows in rec.items():
            f, r = judge(rows, word)
            print(f"  단어 겹침        {name:8} {f:6.1%} / {r:6.1%}")
        fit = rec["dev"] + rec["heldout"]
        thresholds = sorted({round(h[1], 2) for r in fit for h in r["hits"] if h[1] is not None})
        best = max(thresholds, key=lambda t: (sum(judge(fit, lambda h, t=t: h[1] is not None and h[1] >= t)) / 2, -t))
        vec = lambda h, t=best: h[1] is not None and h[1] >= t  # noqa: E731
        print(f"  (벡터 유사도 기준값 {best:.2f}: dev+heldout 으로 선택)")
        for name, rows in rec.items():
            f, r = judge(rows, vec)
            print(f"  벡터 유사도      {name:8} {f:6.1%} / {r:6.1%}")


if __name__ == "__main__":
    main()
