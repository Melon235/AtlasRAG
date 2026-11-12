"""SSRF, redirect, DNS-pinning, and sanitizer boundary tests."""

from __future__ import annotations

from email.message import Message

import pytest

from atlasrag.providers.web.fetch import (
    Resolver,
    SafeHttpFetcher,
    UnsafeWebUrlError,
    WebFetchResponseError,
)
from atlasrag.providers.web.sanitize import sanitize_html


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]


class FakeSocket:
    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.closed = False
        self.sock = FakeSocket()
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def public_fetcher(*, resolver: Resolver | None = None) -> SafeHttpFetcher:
    return SafeHttpFetcher(
        resolver=resolver or (lambda host, port: ("93.184.216.34",)),
        max_bytes=100,
        max_redirects=2,
    )


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "file:///etc/passwd",
        "http://example.com:8080/",
        "http://user@example.com/",
    ),
)
@pytest.mark.asyncio
async def test_unsafe_urls_are_rejected_before_connection(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = public_fetcher()

    def fail(*args: object) -> object:
        raise AssertionError("connected")

    monkeypatch.setattr(fetcher, "_connect", fail)
    with pytest.raises(UnsafeWebUrlError):
        await fetcher.fetch(url)


@pytest.mark.asyncio
async def test_mixed_private_dns_answer_is_rejected() -> None:
    fetcher = public_fetcher(resolver=lambda host, port: ("93.184.216.34", "10.0.0.1"))
    with pytest.raises(UnsafeWebUrlError):
        await fetcher.fetch("https://example.com/")


@pytest.mark.asyncio
async def test_redirect_target_is_revalidated_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = public_fetcher()
    response = FakeResponse(302, {"location": "http://127.0.0.1/"}, b"")
    connection = FakeConnection(response)
    calls: list[tuple[str, int, str, str]] = []

    def connect(address: str, port: int, host: str, scheme: str) -> FakeConnection:
        calls.append((address, port, host, scheme))
        return connection

    monkeypatch.setattr(fetcher, "_connect", connect)
    with pytest.raises(UnsafeWebUrlError):
        await fetcher.fetch("https://example.com/start")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_connection_is_pinned_to_validated_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = public_fetcher()
    connection = FakeConnection(
        FakeResponse(
            200, {"content-type": "text/html; charset=utf-8"}, b"<h1>Hello</h1>"
        )
    )
    seen: list[tuple[str, int, str, str]] = []

    def connect(address: str, port: int, host: str, scheme: str) -> FakeConnection:
        seen.append((address, port, host, scheme))
        return connection

    monkeypatch.setattr(fetcher, "_connect", connect)
    page = await fetcher.fetch("https://example.com/page")
    assert page.content == "<h1>Hello</h1>"
    assert seen and seen[0][0] == "93.184.216.34"
    assert connection.requests[0][2]["Host"] == "example.com"


@pytest.mark.asyncio
async def test_unsupported_content_type_and_oversize_body_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = public_fetcher()
    monkeypatch.setattr(
        fetcher,
        "_connect",
        lambda address, port, host, scheme: FakeConnection(
            FakeResponse(200, {"content-type": "application/octet-stream"}, b"data")
        ),
    )
    with pytest.raises(WebFetchResponseError):
        await fetcher.fetch("https://example.com/file")
    monkeypatch.setattr(
        fetcher,
        "_connect",
        lambda address, port, host, scheme: FakeConnection(
            FakeResponse(200, {"content-type": "text/html"}, b"x" * 101)
        ),
    )
    with pytest.raises(WebFetchResponseError):
        await fetcher.fetch("https://example.com/big")


def test_sanitizer_drops_active_hidden_and_navigation_content() -> None:
    page = sanitize_html(
        "<title>Title</title><nav>menu</nav><script>bad()</script>"
        "<div hidden><p>hidden</p></div><p>Visible <b>text</b></p>",
        max_chars=200,
    )
    assert page.title == "Title"
    assert "Visible text" in page.content
    assert "hidden" not in page.content
    assert "menu" not in page.content
    assert "bad()" not in page.content


class SlowConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__(FakeResponse(200, {"content-type": "text/html"}, b"never"))
        import threading

        self.release = threading.Event()

    def getresponse(self) -> FakeResponse:
        self.release.wait(timeout=2)
        if self.closed:
            raise OSError("aborted by deadline")
        return self.response

    def close(self) -> None:
        self.closed = True
        self.release.set()


@pytest.mark.asyncio
async def test_total_fetch_deadline_aborts_slow_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlasrag.domain.errors import DependencyTimeoutError

    fetcher = public_fetcher()
    monkeypatch.setattr(fetcher, "_connect", lambda *args: SlowConnection())
    with pytest.raises((DependencyTimeoutError, WebFetchResponseError)):
        await fetcher.fetch("https://example.com/slow", timeout_seconds=0.05)


class BadStatusConnection(FakeConnection):
    def getresponse(self) -> FakeResponse:
        import http.client

        raise http.client.BadStatusLine("garbage")


@pytest.mark.asyncio
async def test_malformed_http_status_is_fetch_failure_not_global_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = public_fetcher()
    monkeypatch.setattr(
        fetcher,
        "_connect",
        lambda *args: BadStatusConnection(FakeResponse(200, {}, b"")),
    )
    with pytest.raises(WebFetchResponseError):
        await fetcher.fetch("https://example.com/garbage")
