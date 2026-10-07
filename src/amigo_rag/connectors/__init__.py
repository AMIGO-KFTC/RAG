"""링크(URL) 수집기. 링크 유형을 판별해 컨플루언스 / 나누미 / 일반 웹 수집기로 보낸다."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from ..config import RAGSettings
from ..models import ParsedDocument
from .confluence import fetch_confluence, parse_confluence_url
from .http import fetch_page, make_client, validate_url

LINK_TYPES = ("confluence", "nanumi", "web")


def detect_link_type(url: str, settings: RAGSettings | None = None) -> str:
    settings = settings or RAGSettings()
    host = (urlparse(url).hostname or "").lower()
    for base, kind in ((settings.confluence_base_url, "confluence"), (settings.nanumi_base_url, "nanumi")):
        if base and host == (urlparse(base).hostname or "").lower():
            return kind
    if "atlassian.net" in host or "confluence" in host or re.search(r"/wiki/spaces/|/display/|viewpage\.action", url):
        return "confluence"
    if "nanumi" in host:
        return "nanumi"
    return "web"


def fetch_nanumi(url: str, settings: RAGSettings, client: httpx.Client) -> ParsedDocument:
    """나누미(사내 지식공유 시스템) 페이지 수집.

    현재는 로그인 쿠키/인증 헤더를 붙여 HTML 을 가져오는 범용 방식이다.
    나누미가 API(예: 게시글 JSON)를 제공하면 이 함수만 교체하면 된다.
      - NANUMI_COOKIE      : 브라우저에서 복사한 세션 쿠키 문자열
      - NANUMI_AUTH_HEADER : "Authorization: Bearer xxx" 형식의 헤더
    """
    headers: dict[str, str] = {}
    if settings.nanumi_cookie:
        headers["Cookie"] = settings.nanumi_cookie
    if settings.nanumi_auth_header and ":" in settings.nanumi_auth_header:
        name, _, value = settings.nanumi_auth_header.partition(":")
        headers[name.strip()] = value.strip()
    return fetch_page(client, url, source_type="nanumi", headers=headers)


def fetch_url(
    url: str,
    settings: RAGSettings | None = None,
    *,
    link_type: str | None = None,
    client: httpx.Client | None = None,
) -> ParsedDocument:
    """링크를 가져와 ParsedDocument 로 변환한다. 실패하면 ParseError(사용자에게 보여줄 한국어 메시지)."""
    settings = settings or RAGSettings()
    url = url.strip()
    kind = link_type if link_type in LINK_TYPES else detect_link_type(url, settings)
    own_client = client is None
    client = client or make_client(settings)
    try:
        validate_url(url, settings, check_dns=False)
        if kind == "confluence":
            doc = fetch_confluence(url, settings, client)
        elif kind == "nanumi":
            doc = fetch_nanumi(url, settings, client)
        else:
            doc = fetch_page(client, url, source_type="web")
    finally:
        if own_client:
            client.close()
    doc.metadata.setdefault("url", url)
    return doc


__all__ = ["fetch_url", "detect_link_type", "make_client", "parse_confluence_url", "LINK_TYPES"]
