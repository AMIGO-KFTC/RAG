"""링크 수집용 HTTP 클라이언트와 공통 다운로드 로직.

서버가 사용자가 입력한 URL 을 대신 요청하므로(SSRF 위험) 다음을 막는다.
  - http/https 이외의 스킴
  - 링크로컬(클라우드 메타데이터 169.254.x.x)·멀티캐스트·미지정 주소, 기본적으로 루프백도 차단
  - AMIGO_RAG_ALLOWED_HOSTS 가 설정되어 있으면 그 도메인(하위 도메인 포함)만 허용
리다이렉트되는 모든 요청에도 같은 검사를 적용한다.
"""

from __future__ import annotations

import ipaddress
import socket
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from ..config import RAGSettings
from ..models import ParsedDocument, ParseError
from ..parsers import parse_file, supported_extensions
from ..parsers.html import html_title, html_to_blocks
from ..parsers.text import text_to_blocks
from ..textutil import decode_bytes

USER_AGENT = "AMIGO-Handover-Bot/0.1"
MAX_PAGE_BYTES = 20 * 1024 * 1024


def validate_url(url: str, settings: RAGSettings, *, check_dns: bool = True) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ParseError("http 또는 https 링크만 등록할 수 있습니다.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ParseError("링크 주소에 호스트가 없습니다.")
    if settings.allowed_hosts and not any(host == h or host.endswith("." + h) for h in settings.allowed_hosts):
        raise ParseError(f"허용되지 않은 도메인입니다: {host} (AMIGO_RAG_ALLOWED_HOSTS 확인)")
    if not check_dns:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ParseError(f"주소를 찾을 수 없습니다: {host} (사내망 전용 주소라면 서버가 사내망에 있어야 합니다)") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ParseError(f"접근이 차단된 주소입니다: {host}")
        if ip.is_loopback and not settings.allow_loopback:
            raise ParseError(f"로컬 주소는 등록할 수 없습니다: {host}")


def make_client(settings: RAGSettings, *, transport: httpx.BaseTransport | None = None, check_dns: bool = True) -> httpx.Client:
    def _check(request: httpx.Request) -> None:
        validate_url(str(request.url), settings, check_dns=check_dns)

    return httpx.Client(
        timeout=settings.http_timeout,
        verify=settings.verify_ssl,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [_check]},
        transport=transport,
    )


def download(client: httpx.Client, url: str, *, headers: dict | None = None, limit: int = MAX_PAGE_BYTES) -> tuple[httpx.Response, bytes]:
    try:
        with client.stream("GET", url, headers=headers or {}) as resp:
            if resp.status_code in (401, 403):
                raise ParseError("접근 권한이 없습니다(로그인/권한 필요). 페이지를 PDF 나 HTML 로 저장해 파일로 올려 주세요.")
            if resp.status_code == 404:
                raise ParseError("페이지를 찾을 수 없습니다(404). 링크를 확인해 주세요.")
            if resp.status_code >= 400:
                raise ParseError(f"페이지를 가져오지 못했습니다 (HTTP {resp.status_code}).")
            body = bytearray()
            for part in resp.iter_bytes():
                body += part
                if len(body) > limit:
                    raise ParseError("페이지가 너무 커서 가져올 수 없습니다.")
            return resp, bytes(body)
    except httpx.TimeoutException as exc:
        raise ParseError("링크 응답 시간이 초과되었습니다.") from exc
    except httpx.HTTPError as exc:
        raise ParseError(f"링크에 연결하지 못했습니다: {exc.__class__.__name__}") from exc


def fetch_page(
    client: httpx.Client,
    url: str,
    *,
    source_type: str = "web",
    headers: dict | None = None,
) -> ParsedDocument:
    """URL 을 내려받아 형식(HTML/PDF/텍스트/첨부파일)에 맞게 파싱한다."""
    resp, body = download(client, url, headers=headers)
    final_url = str(resp.url)
    if _looks_like_login(final_url, url):
        raise ParseError("로그인 페이지로 이동했습니다. 인증 정보 설정이 필요하거나, 페이지를 파일로 저장해 올려 주세요.")
    content_type = resp.headers.get("content-type", "").lower()
    path_name = Path(unquote(urlparse(final_url).path)).name

    if "html" in content_type or (not content_type and body.lstrip()[:1] == b"<"):
        html = decode_bytes(body) if "charset" not in content_type else body.decode(resp.encoding or "utf-8", errors="replace")
        title = html_title(html) or path_name or final_url
        doc = ParsedDocument(source_name=title, source_type=source_type, blocks=html_to_blocks(html, drop_chrome=True))
    elif content_type.startswith("text/"):
        doc = ParsedDocument(source_name=path_name or final_url, source_type=source_type, blocks=text_to_blocks(decode_bytes(body)))
    else:
        suffix = Path(path_name).suffix.lower()
        if "pdf" in content_type:
            suffix = ".pdf"
        if suffix not in supported_extensions():
            raise ParseError(f"이 링크의 콘텐츠 형식은 지원하지 않습니다: {content_type or suffix or '알 수 없음'}")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / f"download{suffix}"
            target.write_bytes(body)
            doc = parse_file(target, display_name=path_name or f"download{suffix}")
        doc.source_type = source_type  # 등록 단위는 '링크'. 인용 시 "웹: 파일명 p.3" 으로 표기된다.
    doc.metadata["url"] = final_url
    doc.metadata.setdefault("title", doc.source_name)
    if not any(b.text.strip() for b in doc.blocks):
        doc.warnings.append("페이지에서 본문 텍스트를 찾지 못했습니다(스크립트로 그려지는 페이지일 수 있음).")
    return doc


def _looks_like_login(final_url: str, original: str) -> bool:
    if final_url == original:
        return False
    lowered = final_url.lower()
    return any(k in lowered for k in ("/login", "login.action", "/sso", "signin", "/auth"))
