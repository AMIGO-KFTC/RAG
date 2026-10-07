"""AMIGO 인수인계 시스템 - RAG 모듈.

주요 진입점
    from amigo_rag import KnowledgeBase, search_documents

    kb = KnowledgeBase("session-123")
    kb.add_file("업무정의서.pdf")                       # PDF, HWPX, HWP, DOCX, PPTX, XLSX, EML, 소스코드, ZIP …
    kb.add_url("https://wiki.example.com/pages/viewpage.action?pageId=1")   # 컨플루언스 / 나누미 / 웹
    results = kb.search("홈페이지 담당자", top_k=5)
    results[0].citation  # "업무정의서.pdf p.3"
"""

from .config import RAGSettings
from .connectors import detect_link_type, fetch_url
from .kb import KnowledgeBase, get_knowledge_base, ingest_file, ingest_url, search_documents
from .models import Block, Chunk, IngestResult, ParsedDocument, ParseError, SearchResult, SourceInfo, format_citation
from .parsers import FORMAT_LABELS, parse_file, supported_extensions

__version__ = "0.1.0"

__all__ = [
    "Block",
    "Chunk",
    "FORMAT_LABELS",
    "IngestResult",
    "KnowledgeBase",
    "ParseError",
    "ParsedDocument",
    "RAGSettings",
    "SearchResult",
    "SourceInfo",
    "detect_link_type",
    "fetch_url",
    "format_citation",
    "get_knowledge_base",
    "ingest_file",
    "ingest_url",
    "parse_file",
    "search_documents",
    "supported_extensions",
]
