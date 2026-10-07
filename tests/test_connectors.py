import json

import httpx
import pytest

from amigo_rag import RAGSettings, ParseError, detect_link_type, fetch_url
from amigo_rag.connectors.confluence import parse_confluence_url
from amigo_rag.connectors.http import make_client, validate_url


def test_parse_confluence_urls():
    ref = parse_confluence_url("https://acme.atlassian.net/wiki/spaces/OPS/pages/123456/Batch+Guide")
    assert (ref.base_url, ref.page_id, ref.space_key) == ("https://acme.atlassian.net/wiki", "123456", "OPS")
    ref = parse_confluence_url("https://wiki.corp.local/confluence/pages/viewpage.action?pageId=777")
    assert (ref.base_url, ref.page_id) == ("https://wiki.corp.local/confluence", "777")
    ref = parse_confluence_url("https://wiki.corp.local/display/WEB/%ED%99%88%ED%8E%98%EC%9D%B4%EC%A7%80+%EC%9A%B4%EC%98%81")
    assert (ref.space_key, ref.title) == ("WEB", "홈페이지 운영")


def test_detect_link_type():
    settings = RAGSettings.in_memory()
    settings.nanumi_base_url = "https://nanumi.corp.local"
    assert detect_link_type("https://acme.atlassian.net/wiki/spaces/A/pages/1", settings) == "confluence"
    assert detect_link_type("https://nanumi.corp.local/board/1", settings) == "nanumi"
    assert detect_link_type("https://www.example.com/page", settings) == "web"


def _client(handler, settings):
    return make_client(settings, transport=httpx.MockTransport(handler), check_dns=False)


def test_fetch_confluence_page_via_rest_api():
    settings = RAGSettings.in_memory()
    settings.confluence_pat = "secret-token"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        payload = {
            "id": "777",
            "title": "홈페이지 운영 가이드",
            "space": {"name": "웹서비스팀"},
            "version": {"when": "2026-09-01T10:00:00.000+09:00"},
            "history": {"createdBy": {"displayName": "김민수"}, "createdDate": "2025-01-02T00:00:00Z"},
            "body": {"view": {"value": "<h2>배포 절차</h2><p>매주 목요일 18시에 배포한다.</p>"}},
        }
        return httpx.Response(200, json=payload)

    with _client(handler, settings) as client:
        doc = fetch_url("https://wiki.corp.local/pages/viewpage.action?pageId=777", settings, client=client)
    assert seen["auth"] == "Bearer secret-token"
    assert seen["path"] == "/rest/api/content/777"
    assert doc.source_type == "confluence"
    assert doc.source_name == "홈페이지 운영 가이드"
    assert doc.metadata["space"] == "웹서비스팀" and doc.metadata["author"] == "김민수"
    assert "매주 목요일 18시에 배포한다." in doc.text


def test_fetch_web_page_and_errors():
    settings = RAGSettings.in_memory()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            html = "<html><head><title>공지</title></head><body><nav>메뉴</nav><p>점검 안내입니다.</p></body></html>"
            return httpx.Response(200, html=html)
        if request.url.path == "/login-redirect":
            return httpx.Response(302, headers={"location": "https://intra.example.com/login?next=/x"})
        if request.url.path == "/login":
            return httpx.Response(200, html="<p>로그인</p>")
        return httpx.Response(403)

    with _client(handler, settings) as client:
        doc = fetch_url("https://intra.example.com/ok", settings, client=client)
        assert doc.source_name == "공지" and "점검 안내입니다." in doc.text and "메뉴" not in doc.text
        with pytest.raises(ParseError, match="권한"):
            fetch_url("https://intra.example.com/secret", settings, client=client)
        with pytest.raises(ParseError, match="로그인"):
            fetch_url("https://intra.example.com/login-redirect", settings, client=client)


def test_validate_url_blocks_dangerous_targets():
    settings = RAGSettings.in_memory()
    with pytest.raises(ParseError):
        validate_url("file:///etc/passwd", settings)
    with pytest.raises(ParseError):
        validate_url("http://169.254.169.254/latest/meta-data", settings)
    with pytest.raises(ParseError):
        validate_url("http://127.0.0.1:8000/", settings)
    settings.allow_loopback = True
    validate_url("http://127.0.0.1:8000/", settings)
    settings.allowed_hosts = ["corp.local"]
    with pytest.raises(ParseError, match="허용되지 않은"):
        validate_url("https://evil.example.com/", settings, check_dns=False)
    validate_url("https://wiki.corp.local/x", settings, check_dns=False)


def test_redirect_to_blocked_host_is_rejected():
    settings = RAGSettings.in_memory()
    settings.allowed_hosts = ["corp.local"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/"})

    with _client(handler, settings) as client:
        with pytest.raises(ParseError):
            fetch_url("https://wiki.corp.local/start", settings, client=client)


def test_confluence_json_payload_is_json_serializable_metadata():
    # 메타데이터는 청크로 복사되므로 기본 타입이어야 한다
    settings = RAGSettings.in_memory()

    def handler(request):
        return httpx.Response(200, json={"id": "1", "title": "T", "body": {"storage": {"value": "<p>x</p>"}}})

    with _client(handler, settings) as client:
        doc = fetch_url("https://acme.atlassian.net/wiki/spaces/A/pages/1/T", settings, client=client)
    json.dumps(doc.metadata, ensure_ascii=False)
