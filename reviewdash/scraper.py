"""Walks a product's paginated review pages over plain HTTP.

Review pages are server-rendered on most shops (they're there for search
engines), so this uses httpx rather than a browser. It identifies itself with
an honest user agent, honors robots.txt and Retry-After, and keeps a minimum
gap between requests to the same host.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings
from .extract import ParsedPage, ScrapedReview, parse_page

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_SECONDS = 120


class ScrapeError(Exception):
    pass


@dataclass
class ScrapeResult:
    reviews: list[ScrapedReview] = field(default_factory=list)
    pages: int = 0
    product_name: str | None = None
    methods: set[str] = field(default_factory=set)
    stopped: str = "last_page"  # last_page / caught_up / max_pages / robots / error
    error: str | None = None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class Scraper:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, sleep=asyncio.sleep,
                 today: date | None = None):
        self.settings = settings
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, follow_redirects=True,
        )
        self.sleep = sleep
        self.today = today
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.settings.user_agent, "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en"}

    async def _throttle(self, host: str) -> None:
        gap = self.settings.request_delay_seconds
        last = self._last_request.get(host)
        if last is not None and gap > 0:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                await self.sleep(wait)
        self._last_request[host] = time.monotonic()

    async def allowed(self, url: str) -> bool:
        if not self.settings.respect_robots_txt:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = None
            try:
                await self._throttle(parts.netloc)
                response = await self.client.get(origin + "/robots.txt", headers=self.headers)
                if response.status_code < 400:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
                elif response.status_code in (401, 403):
                    # The robots.txt convention treats a forbidden robots.txt as "disallow everything".
                    parser = RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])
            except httpx.HTTPError as exc:
                log.warning("could not fetch robots.txt for %s: %s", origin, exc)
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(self.settings.user_agent, url)

    async def fetch(self, url: str) -> str:
        host = urlsplit(url).netloc
        attempts = max(1, self.settings.max_attempts)
        for attempt in range(1, attempts + 1):
            await self._throttle(host)
            try:
                response = await self.client.get(url, headers=self.headers)
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise ScrapeError(f"{url}: {exc.__class__.__name__}: {exc}") from exc
                wait = self._backoff(attempt)
                log.info("network error on %s (%s), retry %d in %.1fs", url, exc, attempt, wait)
                await self.sleep(wait)
                continue
            if response.status_code in RETRY_STATUSES and attempt < attempts:
                wait = _retry_after(response)
                wait = self._backoff(attempt) if wait is None else min(wait, MAX_RETRY_AFTER_SECONDS)
                log.info("HTTP %d on %s, retry %d in %.1fs", response.status_code, url, attempt, wait)
                await self.sleep(wait)
                continue
            if response.status_code >= 400:
                raise ScrapeError(f"{url}: HTTP {response.status_code}")
            return response.text
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int) -> float:
        base = max(self.settings.request_delay_seconds, 0.5)
        return base * 2 ** (attempt - 1) * random.uniform(1.0, 1.5)

    async def scrape(self, url: str, selectors: dict | None = None, known_ids: set[str] | None = None,
                     full: bool = False) -> ScrapeResult:
        """Follows "next" links from url. Pages are expected newest-first, so unless full=True
        the walk stops at the first page whose reviews are all already stored."""
        result = ScrapeResult()
        known = known_ids or set()
        seen_urls: set[str] = set()
        next_url: str | None = url
        while next_url:
            if result.pages >= self.settings.max_pages:
                result.stopped = "max_pages"
                break
            if next_url in seen_urls:
                break
            seen_urls.add(next_url)
            if not await self.allowed(next_url):
                result.stopped = "robots"
                result.error = f"robots.txt disallows {next_url}"
                break
            try:
                html = await self.fetch(next_url)
            except ScrapeError as exc:
                result.stopped, result.error = "error", str(exc)
                break
            page: ParsedPage = parse_page(html, next_url, selectors, today=self.today)
            result.pages += 1
            result.product_name = result.product_name or page.product_name
            if page.method:
                result.methods.add(page.method)
            result.reviews.extend(page.reviews)
            log.info("page %d: %d reviews via %s", result.pages, len(page.reviews), page.method or "nothing")
            if not full and page.reviews and all(r.source_id in known for r in page.reviews):
                result.stopped = "caught_up"
                break
            next_url = page.next_url
        return result

def backup_table(data=None):
    if data is None:
        return None
    return data

def parse_input(data=None):
    if data is None:
        return None
    return data