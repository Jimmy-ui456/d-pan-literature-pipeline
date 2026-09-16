from __future__ import annotations

import io
import urllib.error
import unittest

from litsearch.net import HttpClient, NetworkError, ResponseTooLarge, validate_public_http_url


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://example.org/data",
        content_type: str = "application/json",
        content_length: int | None = None,
    ) -> None:
        self._stream = io.BytesIO(body)
        self._url = url
        self.status = 200
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class SequencedOpener:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def open(self, request: object, timeout: int) -> FakeResponse:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


class UrlSafetyTests(unittest.TestCase):
    def test_accepts_public_https(self) -> None:
        self.assertEqual(validate_public_http_url("https://example.org/a"), "https://example.org/a")

    def test_blocks_file_and_local_urls(self) -> None:
        for url in (
            "file:///etc/passwd",
            "http://localhost/a",
            "http://127.0.0.1/a",
            "http://169.254.169.254/latest/meta-data",
        ):
            with self.subTest(url=url), self.assertRaises(NetworkError):
                validate_public_http_url(url)


class HttpClientTests(unittest.TestCase):
    def test_retries_429_then_succeeds(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.org/data",
            429,
            "limited",
            {"Retry-After": "0"},
            None,
        )
        opener = SequencedOpener([error, FakeResponse(b'{"ok": true}')])
        sleeps: list[float] = []
        client = HttpClient(
            email="person@example.org",
            retries=1,
            sleeper=sleeps.append,
            opener=opener,
        )
        self.assertEqual(client.get_json("https://example.org/data"), {"ok": True})
        self.assertEqual(opener.calls, 2)
        self.assertEqual(sleeps, [0.0])

    def test_rejects_large_response_before_read(self) -> None:
        opener = SequencedOpener([FakeResponse(b"x", content_length=1_000)])
        client = HttpClient(email="person@example.org", opener=opener)
        with self.assertRaises(ResponseTooLarge):
            client.request("https://example.org/data", max_bytes=10)


if __name__ == "__main__":
    unittest.main()
