"""명령행 도구. 파서/검색 품질을 빠르게 확인할 때 쓴다.

    python -m amigo_rag parse 업무정의서.hwpx            # 파싱 결과(블록) 확인
    python -m amigo_rag chunks 업무정의서.hwpx           # 청킹 결과 확인
    python -m amigo_rag ingest demo 업무정의서.pdf 메일.eml https://wiki/...   # 지식베이스 적재
    python -m amigo_rag search demo "홈페이지 담당자"      # 검색
    python -m amigo_rag sources demo                     # 적재된 자료 목록
"""

from __future__ import annotations

import argparse
import json
import sys

from .chunking import chunk_document
from .config import RAGSettings
from .kb import KnowledgeBase
from .models import ParseError
from .parsers import parse_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amigo-rag", description="AMIGO RAG 모듈 도구")
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="파일을 파싱해 블록을 출력")
    p_parse.add_argument("path")

    p_chunks = sub.add_parser("chunks", help="파일을 청킹해 결과를 출력")
    p_chunks.add_argument("path")

    p_ingest = sub.add_parser("ingest", help="파일/링크를 지식베이스에 적재")
    p_ingest.add_argument("kb_id")
    p_ingest.add_argument("items", nargs="+", help="파일 경로 또는 http(s) 링크")

    p_search = sub.add_parser("search", help="지식베이스 검색")
    p_search.add_argument("kb_id")
    p_search.add_argument("query")
    p_search.add_argument("-k", "--top-k", type=int, default=5)

    p_sources = sub.add_parser("sources", help="적재된 자료 목록")
    p_sources.add_argument("kb_id")

    args = parser.parse_args(argv)
    settings = RAGSettings()

    try:
        if args.command == "parse":
            doc = parse_file(args.path)
            for part in doc.iter_all():
                print(f"=== {part.source_name} ({part.source_type}) {json.dumps(part.metadata, ensure_ascii=False)}")
                for block in part.blocks:
                    page = f"p{block.page} " if block.page else ""
                    print(f"[{page}{block.kind}{block.level or ''}] {block.text}")
                for warning in part.warnings:
                    print(f"! {warning}")
        elif args.command == "chunks":
            doc = parse_file(args.path)
            for chunk in chunk_document(doc, source_id="preview", chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap):
                print(f"--- {chunk.id} {json.dumps(chunk.metadata, ensure_ascii=False)}")
                print(chunk.text)
        elif args.command == "ingest":
            kb = KnowledgeBase(args.kb_id, settings=settings)
            for item in args.items:
                try:
                    result = kb.add_url(item) if item.startswith(("http://", "https://")) else kb.add_file(item)
                    print(f"✔ {result.source_name}: 청크 {result.chunk_count}개 (source_id={result.source_id})")
                    for warning in result.warnings:
                        print(f"  ! {warning}")
                except ParseError as exc:
                    print(f"✘ {item}: {exc}", file=sys.stderr)
        elif args.command == "search":
            kb = KnowledgeBase(args.kb_id, settings=settings)
            for i, r in enumerate(kb.search(args.query, top_k=args.top_k), start=1):
                print(f"[{i}] {r.citation}  score={r.score} (vec={r.vector_score}, kw={r.keyword_score})")
                print("    " + r.text[:300].replace("\n", "\n    "))
        elif args.command == "sources":
            kb = KnowledgeBase(args.kb_id, settings=settings)
            for s in kb.list_sources():
                print(f"{s.source_id}  {s.source_type:<10} {s.chunk_count:>4}청크  {s.source_name}")
    except ParseError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
