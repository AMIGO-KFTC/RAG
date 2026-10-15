"""검색 품질 평가: samples/ 6종으로 지식베이스를 만들고, 질문별로 올바른 근거를 찾는지/없는 내용을 '없음'으로 말하는지 잰다.

    python evals/retrieval_eval.py            # 요약 표
    python evals/retrieval_eval.py -v         # 질문별 상세(점수 포함)

API 키·네트워크 없이 몇 초 안에 돌아간다(AI/evals 의 종단간 평가와 달리 검색만 잰다).

측정 항목
- 자료에 있는 질문(ANSWERABLE): 정답 문장이 들어 있는 조각을 1위/상위 3위 안에 찾는가(hit@1, hit@3, MRR)
                              그리고 그 정답 조각이 '관련 있음'으로 표시되는가(found)
- 자료에 없는 질문(UNANSWERABLE): 엉뚱한 조각을 근거라며 내놓지 않고 '없음'이라고 판정하는가(rejected)
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amigo_rag import KnowledgeBase, RAGSettings  # noqa: E402

SAMPLES = ROOT / "samples"
SAMPLE_FILES = ["*.pdf", "*.hwpx", "*.docx", "*.eml", "*.xlsx", "*.pptx"]

# (질문, 정답 조각에 들어 있어야 하는 정규식)
ANSWERABLE: list[tuple[str, str]] = [
    ("재무팀 담당자 연락처", r"2345"),
    ("유지보수 대금 지급 요청은 언제 하나요", r"5영업일"),
    ("CMS 권한은 어떻게 신청하나요", r"IT 권한신청서"),
    ("웹방화벽 권한은 누구에게 요청하나요", r"정보보호팀에 메일"),
    ("Google Analytics 권한은 어떻게 넘기나요", r"직접 이관"),
    ("SSL 인증서 만료일이 언제인가요", r"2026-12-15"),
    ("인증서 언제 끝나", r"2026-12-15|12월 중순"),
    ("게시물은 누구 승인이 필요한가요", r"부서장 승인"),
    ("홈페이지 장애가 나면 얼마 안에 보고하나요", r"30분"),
    ("웹 접근성 품질인증 갱신은 몇 월인가요", r"11월"),
    ("개인정보처리방침은 언제까지 게시하나요", r"10/15"),
    ("유지보수 업체는 어디인가요", r"한빛웹솔루션"),
    ("업무별 비중은 어떻게 되나요", r"40%"),
    ("이미지 대체텍스트 누락은 몇 건인가요", r"12건"),
    ("보도자료는 게시 전에 어디 확인을 받나요", r"홍보팀"),
    ("10월 7일까지 무엇을 보내야 하나요", r"검수 결과"),
    ("리뉴얼 예산은 얼마인가요", r"3억"),
    ("매주 월요일에는 무슨 일을 하나요", r"월요일"),
    ("유지보수 업체 장애 대응 시간이 계약 기준을 넘었나요", r"2시간"),
    ("박지훈 차장 이메일 주소", r"jh\.park"),
    ("GA 계정 비밀번호는 어디에 보관하나요", r"보안USB"),
    ("도메인 등록업체 계정은 누가 관리하나요", r"총무팀과 공동"),
    ("주간회의에 누가 참석했나요", r"최유진"),
    ("키보드로 접근이 안 되는 메뉴는 몇 건인가요", r"3건"),
]

# 샘플 자료에 답이 없는 질문. 마지막 둘은 관련 단어(법인카드·서버)는 있지만 답은 없는 '어려운' 경우다.
UNANSWERABLE: list[str] = [
    "퇴사자 계정 회수 절차",
    "ERP 권한 신청 방법",
    "출장비 정산은 어떻게 하나요",
    "연차 휴가 신청 절차",
    "신규 입사자 교육 일정",
    "CRM 시스템 담당자는 누구인가요",
    "개인정보 유출 사고가 나면 어디에 신고하나요",
    "데이터베이스 접속 비밀번호",
    "법인카드 한도는 얼마인가요",
    "서버 백업 주기는 어떻게 되나요",
]


# 개선 작업 전에 따로 만들어 둔 '처음 보는' 질문. 기준값·규칙을 맞출 때는 쓰지 않고 마지막에 확인용으로만 쓴다.
HELDOUT_ANSWERABLE: list[tuple[str, str]] = [
    ("웹접근성 개선 작업은 누가 하나요", r"유지보수 업체 통해"),
    ("인증 갱신이 늦어지면 어떻게 되나요", r"마크 사용이 중지"),
    ("RFP 최종안은 언제까지 공유해야 하나요", r"10/2"),
    ("리뉴얼 예산은 누가 승인하나요", r"이사회"),
    ("웹서비스팀은 어느 부서 소속인가요", r"디지털전략부"),
    ("검수 체크리스트에는 어떤 항목이 있나요", r"장애 처리 건수"),
    ("CMS 관리자 계정은 어떻게 발급받나요", r"신규 발급"),
    ("공용 계정 비밀번호는 어떻게 보관하나요", r"보안USB"),
    ("정보보호팀과는 무엇을 협의하나요", r"웹방화벽 정책"),
    ("IT지원팀과는 무슨 일로 협의하나요", r"서버 및 CMS 계정 발급"),
    ("SSL 인증서는 며칠 전에 갱신을 시작해야 하나요", r"2주"),
    ("장애 보고서는 언제까지 작성하나요", r"다음 영업일"),
    ("접근성 자체 점검은 몇 월에 하나요", r"1·4·7·10월"),
    ("개인정보처리방침 개정은 어디서 검토 중인가요", r"법무팀"),
    ("대금 지급 요청은 어느 부서에 하나요", r"재무팀에 대금 지급 요청"),
    ("명도 대비가 부족한 버튼이 몇 개인가요", r"5건"),
    ("도메인 인증서 비용은 어떻게 결제하나요", r"법인카드"),
    ("주간회의는 어디서 했나요", r"7층 소회의실"),
    ("오래된 공지는 언제 정리하나요", r"월요일"),
    ("계약상 장애 대응 기준 시간은 얼마인가요", r"1시간"),
    ("재무팀이 요청한 제출 기한은 언제인가요", r"10월 7일"),
    ("웹방화벽 콘솔의 권한 수준은 무엇인가요", r"조회"),
    ("1년에 한 번 갱신하는 인증은 무엇인가요", r"품질인증"),
    ("이서연 대리가 맡은 액션 아이템은 무엇인가요", r"웹 접근성 개선 목록"),
]
HELDOUT_UNANSWERABLE: list[str] = [
    "회사 복지 제도는 무엇이 있나요",
    "서버 장애 시 비상 연락망은 어떻게 되나요",
    "SAP 시스템 접근 방법",
    "예산 집행 승인 한도는 얼마인가요",
    "홈페이지 방문자 수 통계는 어디서 보나요",
    "챗봇 도입 계획이 있나요",
    "모바일 앱 운영 담당자는 누구인가요",
    "클라우드 비용 정산 방법",
    "VPN 접속 신청 절차",
    "감사 대응 자료는 어디에 있나요",
    "채용 면접 일정이 어떻게 되나요",
    "백업 서버는 어디에 있나요",
    "개발 서버 접속 계정은 무엇인가요",
    "웹 호스팅 계약 만료일은 언제인가요",
    "Jira 담당자는 누구인가요",
    "정보보호 교육 이수 여부는 어떻게 확인하나요",
]


# 최종 확인 전용 '시험' 세트. 개선 작업이 끝나기 전에는 실행하지 않는다(보고 맞추면 의미가 없어진다).
TEST_ANSWERABLE: list[tuple[str, str]] = [
    ("홈페이지 리뉴얼 사업은 업무 비중이 몇 퍼센트인가요", r"15%"),
    ("키보드로 쓸 수 없는 메뉴가 있나요", r"키보드 접근 불가 메뉴 3건"),
    ("웹 접근성 품질인증 갱신은 어디에 접수하나요", r"인증기관 온라인 접수"),
    ("SSL 인증서는 몇 월에 만료되나요", r"12월 중순|2026-12-15"),
    ("도메인 등록업체는 누구와 공동으로 관리하나요", r"총무팀과 공동 관리"),
    ("게시물 등록은 어떤 순서로 진행하나요", r"담당자 검토 → 부서장 승인"),
    ("9월 대금 지급을 위해 무엇을 보내야 하나요", r"월간 실적 검수 결과"),
    ("회의록에는 장애 대응이 얼마나 걸렸다고 적혀 있나요", r"평균 2시간"),
    ("Google Analytics 접속 주소는 무엇인가요", r"analytics\.google\.com"),
    ("CMS 접속 주소는 무엇인가요", r"cms\.example\.or\.kr"),
    ("업무정의서의 작성 기준일은 언제인가요", r"기준일: 2026년 9월"),
    ("대체텍스트 누락은 언제까지 개선하나요", r"10월 말까지"),
    ("개인정보처리방침은 어디에 게시하나요", r"CMS 게시"),
    ("홈페이지 접속 장애가 나면 어디로 먼저 연락하나요", r"장애 대응 창구"),
    ("재무팀 메일을 참조로 받은 사람은 누구인가요", r"이서연"),
    ("콘텐츠 관리 업무의 주요 내용은 무엇인가요", r"공지사항·보도자료 게시"),
    ("분기별 점검 후에는 무엇을 요청하나요", r"개선 요청"),
    ("월간 검수 때 정기 점검은 어떻게 확인하나요", r"정기 점검 수행 여부 확인"),
    ("리뉴얼 입찰 공고는 언제 나가나요", r"10월 중 입찰 공고"),
    ("보도자료는 게시하기 전에 누가 확인하나요", r"홍보팀 확인"),
]
TEST_UNANSWERABLE: list[str] = [
    "주차장 이용 방법",
    "사내 교육 신청 방법",
    "ISMS 인증 심사 일정은 언제인가요",
    "관리자 페이지 로그는 얼마나 보관하나요",
    "개인정보 접근 이력은 어떻게 점검하나요",
    "홈페이지 트래픽이 급증하면 어떻게 대응하나요",
    "Slack 채널 이름이 무엇인가요",
    "인수자 교육 일정은 어떻게 되나요",
    "출입 보안카드는 어떻게 발급받나요",
    "해외 출장은 어떻게 신청하나요",
    "웹 접근성 인증 비용은 얼마인가요",
    "Oracle 데이터베이스 관리자는 누구인가요",
]


def build_kb() -> KnowledgeBase:
    kb = KnowledgeBase(f"eval-{tempfile.mktemp(prefix='')[-8:]}", settings=RAGSettings.in_memory())
    for pattern in SAMPLE_FILES:
        for path in sorted(SAMPLES.glob(pattern)):
            kb.add_file(path)
    return kb


SETS = {
    "dev": (ANSWERABLE, UNANSWERABLE),
    "heldout": (HELDOUT_ANSWERABLE, HELDOUT_UNANSWERABLE),
    "test": (TEST_ANSWERABLE, TEST_UNANSWERABLE),
}
FOUND = "found(있는 질문을 '있음'으로)"
REJECTED = "rejected(없는 질문을 '없음'으로)"


def evaluate(kb: KnowledgeBase, verbose: bool = False, which: str = "dev") -> dict[str, float]:
    answerable, unanswerable = SETS[which]
    hit1 = hit3 = 0
    mrr = 0.0
    found = 0
    for question, expected in answerable:
        results = kb.search(question, top_k=5)
        rank = next((i + 1 for i, r in enumerate(results) if re.search(expected, r.text)), None)
        hit1 += rank == 1
        hit3 += rank is not None and rank <= 3
        mrr += 1 / rank if rank else 0.0
        # 엄격한 기준: 정답이 들어 있는 바로 그 조각이 '관련 있음'으로 표시돼야 한다
        correct = next((r for r in results if re.search(expected, r.text)), None)
        judged = correct is not None and getattr(correct, "relevant", None) is not False
        found += judged
        if verbose:
            top = results[0] if results else None
            print(f"[있음] rank={rank} found={'Y' if judged else 'N'} {_feat(top)} | {question}")

    rejected = 0
    for question in unanswerable:
        results = kb.search(question, top_k=5)
        judged = _relevant(results)
        rejected += not judged
        if verbose:
            top = results[0] if results else None
            print(f"[없음] rejected={'Y' if not judged else 'N'} {_feat(top)} | {question}")

    n, m = len(answerable), len(unanswerable)
    return {
        "hit@1": hit1 / n,
        "hit@3": hit3 / n,
        "MRR": mrr / n,
        FOUND: found / n,
        REJECTED: rejected / m,
    }


def _relevant(results) -> bool:
    """검색 결과가 '관련 있음'으로 판정됐는지. 판정 기능이 없던 때(기준선)는 결과가 있으면 무조건 '있음'이다."""
    if not results:
        return False
    flags = [getattr(r, "relevant", None) for r in results]
    return True if all(f is None for f in flags) else any(flags)


def _feat(r) -> str:
    if r is None:
        return "(결과 없음)"
    rel = f" rel={r.relevance:.2f}" if getattr(r, "relevance", None) is not None else ""
    return f"score={r.score:.2f} vec={r.vector_score:.2f} kw={r.keyword_score:.2f}{rel}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="질문별 상세 출력")
    parser.add_argument("--set", choices=tuple(SETS), default="dev", help="dev: 개발 중 맞춰 본 질문 / heldout: 검증용 / test: 최종 확인 전용(작업이 끝나기 전에는 실행하지 말 것)")
    parser.add_argument("--sweep", action="store_true", help="min_relevance 기준값을 바꿔 가며 득실을 비교")
    args = parser.parse_args()
    kb = build_kb()
    if args.sweep:
        print("min_relevance  있는 질문 유지(found)  없는 질문 걸러냄(rejected)")
        for threshold in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
            kb.settings.min_relevance = threshold
            r = evaluate(kb, which=args.set)
            print(f"{threshold:13.2f}  {r[FOUND]:19.1%}  {r[REJECTED]:25.1%}")
        return
    n_yes, n_no = len(SETS[args.set][0]), len(SETS[args.set][1])
    print(f"지식베이스: {kb.stats()['chunk_count']} 청크 / 질문 세트 {args.set}: 있음 {n_yes}개, 없음 {n_no}개\n")
    scores = evaluate(kb, args.verbose, args.set)
    print()
    for name, value in scores.items():
        print(f"{name:34} {value:6.1%}" if name != "MRR" else f"{name:34} {value:6.3f}")


if __name__ == "__main__":
    main()
