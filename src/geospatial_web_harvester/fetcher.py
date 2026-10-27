"""Acquire fixture text or bounded HTTP pages before any staging writes.

The HTTP primitive is tested with mocks; it is not a verified adapter for the
example portal. Acquisition never falls back from failed HTTP to synthetic HTML.
"""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .parser import ParsedPage, parse_page


class FetchError(RuntimeError):
    """No successful source observation was obtained."""


def read_fixture(path: Path) -> str:
    """Read UTF-8 fixture HTML; propagate file and decoding errors to the caller."""
    return path.read_text(encoding="utf-8")


class HttpFetcher:
    """Fetch HTML with injectable session and sleep functions for offline tests.

    Construction makes no requests. Callers may supply a context-managed session
    and own its cleanup; an internally created session is exposed as ``session``
    for explicit cleanup. Source access rights and live selectors remain unverified.
    """

    def __init__(
        self,
        timeout: float = 20,
        retries: int = 2,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """Store timeout, additional retries and injectable acquisition dependencies."""
        self.timeout = timeout
        self.retries = retries
        self.session = session if session is not None else requests.Session()
        self.sleep = sleep

    def fetch(self, url: str) -> str:
        """Return nonempty HTML from HTTP 200 or raise FetchError.

        Retry connection/timeouts and server errors up to ``retries`` additional
        times, with exponential waits capped at five seconds. Redirects are not
        followed; authentication denials and rate limits stop immediately.
        Reserved .invalid fixture hosts are rejected before any request. Errors
        omit response bodies and URLs to limit accidental disclosure in logs.
        """
        host = urlsplit(url).hostname or ""
        if host.endswith(".invalid") or host == "invalid":
            raise FetchError(
                "Reserved fixture hosts must never receive real HTTP requests"
            )
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=False,
                    headers={"User-Agent": "GeospatialWebHarvester/0.1"},
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == self.retries:
                    raise FetchError("HTTP connection/timeout failure") from None
            except requests.RequestException:
                raise FetchError("HTTP request failed") from None
            else:
                status = response.status_code
                if status in {401, 403, 429}:
                    raise FetchError(
                        f"HTTP {status}: stop; no access or rate-limit bypass"
                    )
                if status == 200:
                    content_type = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise FetchError("Expected an HTML response from source")
                    if not response.text.strip():
                        raise FetchError("Empty HTTP response")
                    return response.text
                if status < 500 or attempt == self.retries:
                    raise FetchError(f"HTTP {status}: acquisition failed")
            self.sleep(min(2**attempt, 5))
        raise FetchError("Retry limit exhausted")


def fetch_pages(
    first_url: str,
    fetch: Callable[[str], str],
    max_pages: int = 20,
) -> Iterator[ParsedPage]:
    """Yield parsed pages through the injected fetch callable and rel=next links.

    Reject URL cycles, a changed netloc and page-limit exhaustion with FetchError;
    parser failures propagate. Callers must exhaust this iterator successfully
    before persisting, because pages yielded before a later failure do not form
    a complete scan. The CLI materializes all pages to enforce that boundary.
    """
    url = first_url
    seen = set()
    origin = urlsplit(url).netloc
    for _ in range(max_pages):
        if url in seen:
            raise FetchError("Pagination cycle")
        if urlsplit(url).netloc != origin:
            raise FetchError("Pagination changed origin")
        seen.add(url)
        page = parse_page(fetch(url), url)
        yield page
        if not page.next_url:
            return
        url = page.next_url
    raise FetchError("Pagination exceeded configured page limit")
