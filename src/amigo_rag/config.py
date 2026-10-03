"""환경변수 기반 설정. 외부 라이브러리 의존 없이 dataclass 로만 구성한다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [v.strip().lower() for v in value.split(",") if v.strip()]


@dataclass
class RAGSettings:
    # ChromaDB 저장 위치. None 이면 메모리에만 저장(테스트용).
    data_dir: Path | None = field(default_factory=lambda: Path(os.getenv("AMIGO_RAG_DATA_DIR", "./data/rag")))
    # "hash"(기본, 오프라인) 또는 "st:<sentence-transformers 모델명>"
    embedding: str = field(default_factory=lambda: os.getenv("AMIGO_RAG_EMBEDDING", "hash"))
    chunk_size: int = field(default_factory=lambda: _env_int("AMIGO_RAG_CHUNK_SIZE", 900))
    chunk_overlap: int = field(default_factory=lambda: _env_int("AMIGO_RAG_CHUNK_OVERLAP", 150))
    max_file_mb: int = field(default_factory=lambda: _env_int("AMIGO_RAG_MAX_FILE_MB", 50))

    # 링크 수집 (컨플루언스 / 나누미 / 일반 웹)
    http_timeout: int = field(default_factory=lambda: _env_int("AMIGO_RAG_HTTP_TIMEOUT", 20))
    verify_ssl: bool = field(default_factory=lambda: _env_bool("AMIGO_RAG_VERIFY_SSL", True))
    allowed_hosts: list[str] = field(default_factory=lambda: _env_list("AMIGO_RAG_ALLOWED_HOSTS"))
    allow_loopback: bool = field(default_factory=lambda: _env_bool("AMIGO_RAG_ALLOW_LOOPBACK", False))

    confluence_base_url: str = field(default_factory=lambda: os.getenv("CONFLUENCE_BASE_URL", ""))
    confluence_email: str = field(default_factory=lambda: os.getenv("CONFLUENCE_EMAIL", ""))
    confluence_api_token: str = field(default_factory=lambda: os.getenv("CONFLUENCE_API_TOKEN", ""))
    confluence_pat: str = field(default_factory=lambda: os.getenv("CONFLUENCE_PAT", ""))

    nanumi_base_url: str = field(default_factory=lambda: os.getenv("NANUMI_BASE_URL", ""))
    nanumi_cookie: str = field(default_factory=lambda: os.getenv("NANUMI_COOKIE", ""))
    nanumi_auth_header: str = field(default_factory=lambda: os.getenv("NANUMI_AUTH_HEADER", ""))

    @property
    def chroma_dir(self) -> Path | None:
        return None if self.data_dir is None else Path(self.data_dir) / "chroma"

    @classmethod
    def in_memory(cls, **overrides) -> "RAGSettings":
        """디스크에 저장하지 않는 설정(테스트, 일회성 실행)."""
        settings = cls(**overrides)
        settings.data_dir = None
        return settings
