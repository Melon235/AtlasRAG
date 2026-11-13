"""SSRF-resistant HTTP fetch with DNS pinning and redirect revalidation."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from atlasrag.domain.errors import DependencyTimeoutError, DependencyUnavailableError

_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class UnsafeWebUrlError(ValueError):
    """A URL or resolved address violates the fetch security policy."""


class WebFetchResponseError(ValueError):
    """A reachable URL returned an unusable response."""


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    content: str


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    port: int
    path_query: str
    addresses: tuple[str, ...]


Resolver = Callable[[str, int], tuple[str, ...]]


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror:
        raise DependencyUnavailableError from None
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise DependencyUnavailableError
    return tuple(addresses)


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Reject the entire IPv4-compatible ::/96 space, including
        # ::127.0.0.1, which some Python versions mark global/reserved.
        if address.packed[:12] == b"\x00" * 12:
            return False
        # Reject IPv4-mapped/6to4/Teredo alternatives and the well-known NAT64
        # prefix so an embedded private IPv4 cannot bypass the IPv6 check.
        if (
            address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
        ):
            return False
        if address.packed.startswith(bytes.fromhex("0064ff9b")):
            return False
    return True


class SafeHttpFetcher:
    """One bounded request at a time; connection is pinned to a validated IP."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        max_bytes: int = 1_000_000,
        max_redirects: int = 5,
        resolver: Resolver | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("fetch timeout must be within (0, 30]")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 10_000_000:
            raise ValueError("max_bytes must be within [1, 10000000]")
        if type(max_redirects) is not int or not 0 <= max_redirects <= 10:
            raise ValueError("max_redirects must be within [0, 10]")
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver or _default_resolver

    async def fetch(
        self, url: str, *, timeout_seconds: float | None = None
    ) -> FetchedPage:
        timeout = self._timeout if timeout_seconds is None else timeout_seconds
        if not 0 < timeout <= 30:
            raise ValueError("fetch timeout must be within (0, 30]")
        active: set[http.client.HTTPConnection] = set()
        lock = threading.Lock()
        timed_out = threading.Event()

        def abort() -> None:
            timed_out.set()
            with lock:
                connections = tuple(active)
            for connection in connections:
                try:
                    connection.close()
                except OSError:
                    pass

        timer = threading.Timer(timeout, abort)
        timer.daemon = True
        timer.start()
        try:
            return await asyncio.to_thread(self._fetch_sync, url, timeout, active, lock)
        except (OSError, http.client.HTTPException):
            if timed_out.is_set():
                raise DependencyTimeoutError from None
            raise WebFetchResponseError("malformed HTTP response") from None
        finally:
            timer.cancel()

    def _fetch_sync(
        self,
        url: str,
        timeout: float,
        active: set[http.client.HTTPConnection],
        lock: threading.Lock,
    ) -> FetchedPage:
        deadline = time.monotonic() + timeout
        current = self._validate_and_resolve(url)
        visited: set[str] = set()
        for _ in range(self._max_redirects + 1):
            if time.monotonic() >= deadline:
                raise DependencyTimeoutError
            if current.url in visited:
                raise WebFetchResponseError("redirect loop")
            visited.add(current.url)
            status, headers, body = self._request_once(current, timeout, active, lock)
            if status in _REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise WebFetchResponseError("redirect missing location")
                target = urljoin(current.url, location)
                current = self._validate_and_resolve(target)
                continue
            if not 200 <= status < 300:
                raise WebFetchResponseError("non-success HTTP status")
            return FetchedPage(url=current.url, content=body)
        raise WebFetchResponseError("redirect budget exhausted")

    def _validate_and_resolve(self, value: str) -> ValidatedUrl:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise UnsafeWebUrlError("invalid URL text")
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise UnsafeWebUrlError("invalid URL") from None
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            raise UnsafeWebUrlError("unsupported URL scheme")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeWebUrlError("URL userinfo is forbidden")
        host = parsed.hostname
        if host is None or not host or host.endswith(".") or "\\" in host:
            raise UnsafeWebUrlError("invalid URL host")
        try:
            port = parsed.port
        except ValueError:
            raise UnsafeWebUrlError("invalid URL port") from None
        if port is None:
            port = 443 if scheme == "https" else 80
        if port not in {80, 443}:
            raise UnsafeWebUrlError("unsupported URL port")
        host = host.casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal")
        ):
            raise UnsafeWebUrlError("internal hostname")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not _is_public_address(str(literal)):
                raise UnsafeWebUrlError("non-public IP address")
            addresses: tuple[str, ...] = (str(literal),)
            canonical_host = str(literal)
        else:
            if "." not in host or any(not label for label in host.split(".")):
                raise UnsafeWebUrlError("host must be a qualified DNS name")
            try:
                ascii_host = host.encode("idna").decode("ascii").casefold()
            except UnicodeError:
                raise UnsafeWebUrlError("invalid IDNA hostname") from None
            if len(ascii_host) > 253:
                raise UnsafeWebUrlError("hostname is too long")
            canonical_host = ascii_host
            addresses = self._resolver(canonical_host, port)
        if not addresses or any(not _is_public_address(item) for item in addresses):
            raise UnsafeWebUrlError("resolved address is not public")
        host_literal = (
            f"[{canonical_host}]" if ":" in canonical_host else canonical_host
        )
        netloc = host_literal if port in {80, 443} else f"{host_literal}:{port}"
        path_query = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        canonical_url = urlunsplit(
            (scheme, netloc, parsed.path or "/", parsed.query, "")
        )
        return ValidatedUrl(
            url=canonical_url,
            scheme=scheme,
            host=canonical_host,
            port=port,
            path_query=path_query,
            addresses=tuple(addresses),
        )

    def _connect(
        self, address: str, port: int, host: str, scheme: str, timeout: float
    ) -> http.client.HTTPConnection:
        raw_socket = socket.create_connection((address, port), timeout=timeout)
        if scheme == "https":
            context = ssl.create_default_context()
            tls_socket = context.wrap_socket(raw_socket, server_hostname=host)
            raw_socket.close()
            connection = http.client.HTTPConnection(host, port, timeout=self._timeout)
            connection.sock = tls_socket
            return connection
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.sock = raw_socket
        return connection

    def _request_once(
        self,
        validated: ValidatedUrl,
        timeout: float,
        active: set[http.client.HTTPConnection],
        lock: threading.Lock,
    ) -> tuple[int, http.client.HTTPMessage, str]:
        address = validated.addresses[0]
        connection = self._connect(
            address, validated.port, validated.host, validated.scheme, timeout
        )
        connection.sock.settimeout(timeout)
        with lock:
            active.add(connection)
        try:
            connection.request(
                "GET",
                validated.path_query,
                headers={
                    "Host": (
                        f"[{validated.host}]"
                        if ":" in validated.host and validated.port in {80, 443}
                        else (
                            f"[{validated.host}]:{validated.port}"
                            if ":" in validated.host
                            else (
                                validated.host
                                if validated.port in {80, 443}
                                else f"{validated.host}:{validated.port}"
                            )
                        )
                    ),
                    "Accept": "text/html, application/xhtml+xml, text/plain",
                    "Accept-Encoding": "identity",
                    "User-Agent": "AtlasRAG/1.0 (+local evidence fetch)",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = response.status
            headers = response.headers
            if status in _REDIRECT_STATUSES:
                response.read(0)
                return status, headers, ""
            if not 200 <= status < 300:
                response.read(0)
                return status, headers, ""
            content_type = (
                headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            if content_type not in _ALLOWED_CONTENT_TYPES:
                raise WebFetchResponseError("unsupported content type")
            encoding = headers.get("content-encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise WebFetchResponseError("content encoding is not accepted")
            declared = headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > self._max_bytes:
                        raise WebFetchResponseError("response is too large")
                except ValueError:
                    raise WebFetchResponseError("invalid content length") from None
            body = response.read(self._max_bytes + 1)
            if len(body) > self._max_bytes:
                raise WebFetchResponseError("response is too large")
            charset = headers.get_content_charset() or "utf-8"
            try:
                text = body.decode(charset, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            if "\x00" in text:
                text = text.replace("\x00", "")
            return status, headers, text
        finally:
            with lock:
                active.discard(connection)
            connection.close()
