"""컨플루언스 페이지 수집.

지원 URL 형식
  - Cloud  : https://<회사>.atlassian.net/wiki/spaces/<SPACE>/pages/<ID>/<제목>
  - Server : https://confluence.<회사>/pages/viewpage.action?pageId=<ID>
  - Server : https://confluence.<회사>/display/<SPACE>/<제목>
인증
  - Server/Data Center : CONFLUENCE_PAT (개인 액세스 토큰, Bearer)
  - Cloud              : CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN (Basic)
본문은 REST API(/rest/api/content) 의 body.view(렌더링된 HTML)를 우선 사용한다.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote_plus, urlparse

import httpx

from ..config import RAGSettings
from ..models import ParsedDocument, ParseError
from ..parsers.html import html_to_blocks
from ..textutil import clean_text
from .http import download, fetch_page

_EXPAND = "body.view,body.storage,version,space,history,history.lastUpdated,ancestors"


@dataclass
class ConfluenceRef:
    base_url: str
    page_id: str | None = None
    space_key: str | None = None
    title: str | None = None


def parse_confluence_url(url: str, base_hint: str = "") -> ConfluenceRef:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path

    def base(prefix: str) -> str:
        if base_hint and urlparse(base_hint).netloc == parsed.netloc:
            return base_hint.rstrip("/")
        return (origin + prefix).rstrip("/")

    match = re.search(r"^(?P<prefix>.*?)/spaces/(?P<space>[^/]+)/pages/(?P<id>\d+)", path)
    if match:
        return ConfluenceRef(base(match.group("prefix")), match.group("id"), match.group("space"))

    query = parse_qs(parsed.query)
    if "pageId" in query:
        prefix = path.split("/pages/")[0] if "/pages/" in path else ""
        return ConfluenceRef(base(prefix), query["pageId"][0])

    match = re.search(r"^(?P<prefix>.*?)/display/(?P<space>[^/]+)/(?P<title>[^/?#]+)", path)
    if match:
        return ConfluenceRef(base(match.group("prefix")), None, match.group("space"), unquote_plus(match.group("title")))

    return ConfluenceRef(base(""))


def auth_headers(settings: RAGSettings) -> dict[str, str]:
    if settings.confluence_pat:
        return {"Authorization": f"Bearer {settings.confluence_pat}"}
    if settings.confluence_email and settings.confluence_api_token:
        token = base64.b64encode(f"{settings.confluence_email}:{settings.confluence_api_token}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def fetch_confluence(url: str, settings: RAGSettings, client: httpx.Client) -> ParsedDocument:
    ref = parse_confluence_url(url, settings.confluence_base_url)
    headers = {"Accept": "application/json", **auth_headers(settings)}

    if ref.page_id:
        api = f"{ref.base_url}/rest/api/content/{ref.page_id}?expand={_EXPAND}"
        data = _get_json(client, api, headers)
    elif ref.space_key and ref.title:
        api = (
            f"{ref.base_url}/rest/api/content?spaceKey={quote(ref.space_key)}"
            f"&title={quote(ref.title)}&expand={_EXPAND}"
        )
        results = _get_json(client, api, headers).get("results") or []
        if not results:
            raise ParseError(f"컨플루언스에서 '{ref.title}' 페이지를 찾지 못했습니다.")
        data = results[0]
    else:
        # 짧은 링크(/x/abc) 등 해석할 수 없는 형식은 일반 웹 페이지로 시도한다.
        doc = fetch_page(client, url, source_type="confluence", headers=auth_headers(settings))
        return doc

    title = clean_text(data.get("title") or "컨플루언스 페이지")
    body = data.get("body") or {}
    html = (body.get("view") or {}).get("value") or (body.get("storage") or {}).get("value") or ""
    history = data.get("history") or {}
    doc = ParsedDocument(source_name=title, source_type="confluence", blocks=html_to_blocks(html))
    ancestors = [clean_text(a.get("title", "")) for a in data.get("ancestors") or [] if a.get("title")]
    doc.metadata.update(
        {
            "title": title,
            "url": url,
            "page_id": str(data.get("id", ref.page_id or "")),
            "space": clean_text(((data.get("space") or {}).get("name")) or ref.space_key or ""),
            "author": clean_text(((history.get("createdBy") or {}).get("displayName")) or ""),
            "created_at": str(history.get("createdDate") or "")[:10],
            "modified_at": str(((data.get("version") or {}).get("when")) or "")[:10],
            "last_modified_by": clean_text((((history.get("lastUpdated") or {}).get("by") or {}).get("displayName")) or ""),
            "breadcrumb": " > ".join(ancestors),
        }
    )
    if not doc.blocks:
        doc.warnings.append("컨플루언스 페이지 본문이 비어 있습니다.")
    return doc


def _get_json(client: httpx.Client, url: str, headers: dict) -> dict:
    resp, body = download(client, url, headers=headers)
    if "json" not in resp.headers.get("content-type", ""):
        raise ParseError("컨플루언스 API 응답이 JSON 이 아닙니다(로그인 페이지로 이동했을 수 있음). 인증 설정을 확인해 주세요.")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ParseError("컨플루언스 API 응답을 해석하지 못했습니다.") from exc
