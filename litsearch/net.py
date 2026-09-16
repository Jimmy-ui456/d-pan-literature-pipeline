from __future__ import annotations

import email.utils
import ipaddress
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class NetworkError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ResponseTooLarge(NetworkError):
    pass


def validate_public_http_url(url: str) -> str:
    """Reject local/file URLs supplied by untrusted scholarly metadata."""

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise NetworkError("invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise NetworkError("only absolute HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise NetworkError("URLs containing user information are not allowed")
    if any(ord(char) < 32 for char in url) or "\\" in url:
        raise NetworkError("control characters and backslashes are not allowed in URLs")
    try:
        port = parsed.port
    except ValueError as exc:
        raise NetworkError("invalid URL port") from exc
    if port not in {None, 80, 443}:
        raise NetworkError("only standard web ports are allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise NetworkError("local network destinations are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        raise NetworkError("private or special-purpose network destinations are not allowed")
    return urllib.parse.urlunsplit(parsed)


def redact_url(url: str) -> str:
    """No API key, mail address or signed URL query values in diagnostics."""
    try:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.hostname or "", parts.path,
                                        "REDACTED" if parts.query else "", ""))
    except ValueError:
        return "[invalid URL]"


def _public_connection(address: tuple[str, int], timeout: float = 30,
                       source_address: Any = None, **kwargs: Any) -> socket.socket:
    """Resolve once, reject every non-global address, connect to that exact IP.

    Checking DNS and then asking urllib to resolve again permits DNS rebinding.
    TLS still uses the original hostname for SNI and certificate verification.
    """
    hostname, port = address
    entries = socket.getaddrinfo(hostname, port, 0, socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(entry[4][0]) for entry in entries]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses):
        raise NetworkError("DNS resolved to a non-public destination")
    last_error: OSError | None = None
    for family, kind, proto, _, sockaddr in entries:
        connection = socket.socket(family, kind, proto)
        try:
            connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    raise last_error or NetworkError("no public destination could be reached")


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: Any) -> Any:
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: Any) -> Any:
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class Response:
    body: bytes
    content_type: str
    final_url: str
    status: int


class HttpClient:
    """Small urllib client with bounded reads, safe redirects, retries and backoff."""

    def __init__(
        self,
        *,
        email: str,
        retries: int = 3,
        backoff_seconds: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Any | None = None,
    ) -> None:
        self.email = email.strip()
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self._last_request_at = 0.0
        self._throttle_lock = threading.Lock()
        self.sleeper = sleeper
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(), SafeRedirectHandler()
        )
        contact = self.email or "contact-not-configured"
        self.user_agent = f"metabolic-literature-pipeline/3.0 (mailto:{contact})"

    def _retry_delay(self, attempt: int, headers: Any | None = None) -> float:
        if headers is not None:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    try:
                        parsed = email.utils.parsedate_to_datetime(retry_after)
                        return max(0.0, parsed.timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass
        return min(30.0, self.backoff_seconds * (2**attempt))

    @staticmethod
    def _read_bounded(response: Any, max_bytes: int) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ResponseTooLarge(
                        f"response Content-Length {content_length} exceeds {max_bytes} bytes"
                    )
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(f"response exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def request(
        self,
        url: str,
        *,
        timeout: int = 30,
        max_bytes: int = 20 * 1024 * 1024,
        accept: str = "*/*",
        data: bytes | None = None,
    ) -> Response:
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < 0.15:
                self.sleeper(0.15 - elapsed)
            self._last_request_at = time.monotonic()
        safe_url = validate_public_http_url(url)
        request = urllib.request.Request(
            safe_url,
            data=data,
            headers={"User-Agent": self.user_agent, "Accept": accept},
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    status = int(getattr(response, "status", 200))
                    body = self._read_bounded(response, max_bytes)
                    return Response(
                        body=body,
                        content_type=response.headers.get("Content-Type", ""),
                        final_url=validate_public_http_url(response.geturl()),
                        status=status,
                    )
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS or attempt >= self.retries:
                    break
                delay = self._retry_delay(attempt, exc.headers)
                if delay > 60:
                    # Do not retry earlier than the server asked or block for minutes.
                    break
                self.sleeper(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                self.sleeper(self._retry_delay(attempt))
        raise NetworkError(f"request failed after {attempt + 1} attempt(s): {redact_url(safe_url)}",
                           status=getattr(last_error, "code", None)) from last_error

    def get_json(self, url: str, *, timeout: int = 40) -> dict[str, Any]:
        response = self.request(
            url,
            timeout=timeout,
            max_bytes=25 * 1024 * 1024,
            accept="application/json",
        )
        try:
            value = json.loads(response.body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError(f"invalid JSON from {redact_url(response.final_url)}") from exc
        if not isinstance(value, dict):
            raise NetworkError(f"expected a JSON object from {redact_url(response.final_url)}")
        return value

    def get_text(self, url: str, *, timeout: int = 30) -> tuple[str, str]:
        response = self.request(
            url,
            timeout=timeout,
            max_bytes=5 * 1024 * 1024,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
        )
        charset = "utf-8"
        for part in response.content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip("\"'")
        try:
            text = response.body.decode(charset, errors="replace")
        except LookupError:
            text = response.body.decode("utf-8", errors="replace")
        return text, response.final_url

    def get_bytes(
        self,
        url: str,
        *,
        timeout: int = 30,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> Response:
        return self.request(
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            accept="application/pdf,application/octet-stream;q=0.9,*/*;q=0.2",
        )

    def post_bytes(
        self,
        url: str,
        data: bytes,
        *,
        timeout: int = 60,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> Response:
        return self.request(
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            accept="application/xml,text/xml;q=0.9,*/*;q=0.2",
            data=data,
        )
