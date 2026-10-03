# AMIGO RAG — 문서 파싱 · 청킹 · 벡터 검색 모듈

AI 기반 인수인계서 자동 작성 시스템의 **데이터/RAG 계층**입니다.
업무 자료(PDF·한글·Word·메일·소스코드·컨플루언스/나누미 링크 …)를 텍스트로 변환하고, 의미 단위로 쪼개 출처 꼬리표를 붙인 뒤
ChromaDB 에 저장하며, AI 에이전트가 호출할 `search_documents()` 검색 함수를 제공합니다.

```
FrontEnd(React) ──HTTP──▶ BackEnd(FastAPI) ──import──▶ AI(LangGraph 에이전트)
                                   │                          │ search_documents()
                                   └──────── import ──────────▼
                                                       RAG(이 저장소, ChromaDB)
```

| 저장소 | 담당 | 역할 |
|---|---|---|
| FrontEnd | 가회 | 업로드 UI, 진행 단계 표시, 채팅, 문서 뷰어 |
| BackEnd | 소영 | FastAPI, 세션/대화 DB, 파일 저장 → RAG 적재 |
| AI | 윤희 | LangGraph STAGE 1~4, 질문 생성, 도구 호출, 문서 합성 |
| **RAG** | **환** | **파싱, 청킹·메타데이터, ChromaDB, 검색 함수** |

## 빠른 시작

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python samples/make_samples.py          # 가상의 인수인계 샘플 자료 6종 생성(이미 samples/ 에 포함)
python -m amigo_rag ingest demo samples/*.pdf samples/*.hwpx samples/*.docx samples/*.eml samples/*.xlsx samples/*.pptx
python -m amigo_rag search demo "CMS 권한 신청 방법"
pytest -q
```

## 파이썬 API (AI · BackEnd 팀이 쓰는 인터페이스)

```python
from amigo_rag import KnowledgeBase, search_documents

kb = KnowledgeBase("session-123")                 # 인수인계 세션 1개 = 지식베이스 1개(Chroma 컬렉션)
kb.add_file("/tmp/업무정의서.pdf", source_id="src-1")   # 파일 적재 → IngestResult(chunk_count, warnings …)
kb.add_url("https://wiki.example.com/pages/viewpage.action?pageId=123")  # 컨플루언스/나누미/웹 링크
kb.add_text("유지보수 업체 담당자는 …", source_name="인계자 답변", source_type="chat")  # 대화로 얻은 정보

results = kb.search("홈페이지 담당자", top_k=5)    # list[SearchResult]
r = results[0]
r.text        # 청크 원문
r.citation    # "업무정의서.pdf p.3", "메일 '월간보고' (박지훈, 2026-09-30)", "컨플루언스: 운영 가이드"
r.score       # 0~1 관련도 (벡터 유사도와 키워드 일치율의 평균)
r.metadata    # 출처 메타데이터(아래 표)

# 모듈 함수 형태(설정은 환경변수 사용)
search_documents("홈페이지 담당자", kb_id="session-123", top_k=5)
```

검색 옵션: `source_types=["pdf","eml"]`, `source_ids=[...]`, `where={...}`(Chroma 문법), `per_source_limit=2`(자료별 최대 결과 수).
관리: `list_sources()`, `delete_source(source_id)`, `get_chunks(source_id)`, `stats()`, `clear()`.

## 지원 형식

| 형식 | 처리 방식 |
|---|---|
| PDF | pdfplumber 로 레이아웃(줄·제목·표) 분석. pdfminer 가 글자를 빠뜨리는 페이지는 PDFium 글자 좌표로 재구성, `(cid:…)` 깨짐은 PDFium 전체 텍스트로 대체. 스캔 페이지는 경고 |
| HWPX | ZIP+XML(OWPML) 직접 해석: spine 순서, `개요 N` 스타일 → 제목, 표 → 마크다운, 글상자·각주 포함, 실패 시 미리보기 텍스트 |
| HWP(5.0) | OLE 스트림 → raw-deflate 해제 → `PARA_TEXT` 레코드 디코딩. 암호 문서는 안내, 배포용 문서는 미리보기 텍스트 |
| DOCX / PPTX / XLSX | 본문 순서대로 문단·표, 제목 스타일, 목록 / 슬라이드 제목·표·발표자 노트 / 시트별 표(머리글 반복) |
| EML / MSG | 발신자·수신자·참조·일시·제목 메타데이터, HTML 본문 정리, **지원 형식 첨부파일 재귀 파싱**, `ks_c_5601-1987` 등 레거시 문자셋 |
| 소스코드·텍스트·MD·CSV | 확장자별 언어 표시, 줄바꿈 보존 분할 / CP949·UTF-8 자동 판별 |
| ZIP | 소스 묶음·자료 모음. 항목 수/용량 제한, CP949 파일명 복원, `node_modules`·`.git` 등 제외 |
| 링크 | 컨플루언스(REST API, Cloud/Server URL 모두), 나누미(쿠키/헤더 인증 범용 수집기), 일반 웹(HTML/PDF/텍스트) |

새 형식은 `(Path) -> ParsedDocument` 함수를 만들어 `parsers/__init__.py` 의 `PARSERS` 에 확장자를 등록하면 됩니다.

## 청킹과 메타데이터

- 제목이 나오면 새 청크를 시작하고 `section`(예: `업무정의서 > 2. 주요 업무`)을 남깁니다.
- 표는 통째로 유지, 크면 머리글 행을 반복하며 행 단위 분할. 긴 문단은 문장 단위 분할 + 앞 청크 끝부분 overlap.
- 기본 `chunk_size=900`자, `chunk_overlap=150`자(한국어 기준 대략 400~600 토큰).

| 메타데이터 키 | 의미 |
|---|---|
| `source_id`, `source_name`, `source_type` | 등록 단위(업로드한 파일/링크) |
| `file_name`, `file_type`, `parent_name` | 실제 텍스트가 나온 파일(메일 첨부·ZIP 내부 파일이면 부모 이름 포함) |
| `page`, `page_end`, `sheet`, `section`, `chunk_index` | 위치 정보 |
| `sender`, `sender_name`, `recipients`, `cc`, `sent_at`, `subject` | 메일(첨부 청크도 상속) |
| `url`, `space`, `author`, `created_at`, `modified_at`, `title`, `language` | 링크·문서 속성 |

## 검색 방식

- **오프라인 기본 임베딩**: 한글 음절 2/3-gram + 단어 해싱(1024차원). 모델 다운로드가 필요 없어 사내망에서도 바로 동작합니다.
- **하이브리드 검색**: ChromaDB 벡터 검색 + BM25(한글 2-gram 토큰) → RRF 로 순위 융합. 사람 이름·시스템명 같은 고유명사에 강합니다.
- 의미 검색 품질을 높이려면 `pip install -e ".[st]"` 후 `AMIGO_RAG_EMBEDDING=st:jhgan/ko-sroberta-multitask`(또는 `st:BAAI/bge-m3`).
  임베딩 설정을 바꾸면 기존 지식베이스는 다시 적재해야 합니다(혼용 시 오류로 알려 줍니다).

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `AMIGO_RAG_DATA_DIR` | `./data/rag` | ChromaDB 저장 위치(`<dir>/chroma`) |
| `AMIGO_RAG_EMBEDDING` | `hash` | `hash` 또는 `st:<모델명>` |
| `AMIGO_RAG_CHUNK_SIZE` / `AMIGO_RAG_CHUNK_OVERLAP` | `900` / `150` | 청크 크기(문자) |
| `AMIGO_RAG_MAX_FILE_MB` | `50` | 파일 크기 제한 |
| `CONFLUENCE_BASE_URL` | | 컨플루언스 주소(링크 유형 판별·컨텍스트 경로) |
| `CONFLUENCE_PAT` | | Server/DC 개인 액세스 토큰 |
| `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN` | | Cloud 인증 |
| `NANUMI_BASE_URL`, `NANUMI_COOKIE`, `NANUMI_AUTH_HEADER` | | 나누미 주소와 인증 |
| `AMIGO_RAG_ALLOWED_HOSTS` | | 링크 수집 허용 도메인(쉼표 구분, 비우면 전체 허용) |
| `AMIGO_RAG_ALLOW_LOOPBACK` | `false` | 로컬 주소 링크 허용(개발용) |
| `AMIGO_RAG_HTTP_TIMEOUT`, `AMIGO_RAG_VERIFY_SSL` | `20`, `true` | 링크 수집 HTTP 설정 |

링크 수집은 서버가 대신 요청하므로 http/https 만 허용하고 링크로컬(169.254.x.x)·멀티캐스트·루프백 주소를 차단하며, 리다이렉트마다 다시 검사합니다.

## 다음 단계(확장 지점)

- **OCR**: 스캔 PDF/이미지(현재는 경고만). `parsers/pdf.py` 의 빈 페이지 처리 지점에 Tesseract 또는 비전 모델 연결
- **나누미 API**: 게시글 API 가 확정되면 `connectors/__init__.py::fetch_nanumi` 만 교체
- **임베딩 모델**: 사내 GPU/모델 서버가 있으면 `embeddings.py` 에 클래스 추가 후 `get_embedder` 에 등록
