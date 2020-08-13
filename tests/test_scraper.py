import httpx
import pytest

from demo_store.server import PLAIN_SELECTORS
from reviewdash.scraper import Scraper

from .conftest import STORE, TODAY, make_settings


def scraper_for(handler, sleeps, **settings):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://shop.test")
    return Scraper(make_settings(**settings), client=client, sleep=sleeps, today=TODAY)


def review_page(ids, next_href=None):
    cards = "".join(
        f'<div itemscope itemtype="https://schema.org/Review" id="{i}"><p itemprop="reviewBody">Review {i}</p></div>'
        for i in ids
    )
    link = f'<link rel="next" href="{next_href}">' if next_href else ""
    return f"<html><head>{link}</head><body>{cards}</body></html>"


@pytest.mark.parametrize("slug, method, count", [
    ("aurora-anc-headphones", "microdata", 108),
    ("ember-gooseneck-kettle", "json-ld", 64),
    ("nimbus-robot-vacuum", "microdata", 64),
])
async def test_reads_every_page_of_the_demo_store(http, store, sleeps, slug, method, count):
    scraper = Scraper(make_settings(), client=http, sleep=sleeps, today=TODAY)
    result = await scraper.scrape(f"{STORE}/products/{slug}")
    product = store.state.catalog[slug]
    assert len(product.reviews) == count
    assert result.stopped == "last_page"
    assert result.methods == {method}
    assert result.product_name == product.name
    assert [r.source_id for r in result.reviews] == [r.id for r in product.reviews]
    truth = product.reviews[0]
    first = result.reviews[0]
    assert (first.rating, first.published_on, first.author, first.title, first.body) == (
        truth.rating, truth.published, truth.author, truth.title, truth.body)


async def test_plain_html_needs_the_selectors(http, store, sleeps):
    scraper = Scraper(make_settings(), client=http, sleep=sleeps, today=TODAY)
    url = f"{STORE}/products/ridgeline-trail-boots"
    without = await scraper.scrape(url)
    assert without.reviews == [] and without.pages == 1
    with_selectors = await scraper.scrape(url, PLAIN_SELECTORS)
    assert len(with_selectors.reviews) == len(store.state.catalog["ridgeline-trail-boots"].reviews)
    assert with_selectors.methods == {"selectors"}


async def test_incremental_scrape_stops_at_known_reviews(http, store, sleeps):
    scraper = Scraper(make_settings(), client=http, sleep=sleeps, today=TODAY)
    url = f"{STORE}/products/ember-gooseneck-kettle"
    first = await scraper.scrape(url)
    known = {r.source_id for r in first.reviews}
    again = await scraper.scrape(url, known_ids=known)
    assert again.stopped == "caught_up" and again.pages == 1
    full = await scraper.scrape(url, known_ids=known, full=True)
    assert full.pages == first.pages


async def test_robots_txt_is_honoured(sleeps):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text=review_page(["a"]))

    scraper = scraper_for(handler, sleeps)
    blocked = await scraper.scrape("http://shop.test/private/reviews")
    assert blocked.stopped == "robots" and blocked.pages == 0 and "robots.txt" in blocked.error
    assert (await scraper.scrape("http://shop.test/public")).pages == 1

    ignoring = scraper_for(handler, sleeps, respect_robots_txt=False)
    assert (await ignoring.scrape("http://shop.test/private/reviews")).pages == 1


async def test_forbidden_robots_txt_means_disallow_all_and_missing_means_allow(sleeps):
    def forbidden(request):
        return httpx.Response(403 if request.url.path == "/robots.txt" else 200, text=review_page(["a"]))

    def missing(request):
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text=review_page(["a"]))

    assert (await scraper_for(forbidden, sleeps).scrape("http://shop.test/p")).stopped == "robots"
    assert (await scraper_for(missing, sleeps).scrape("http://shop.test/p")).pages == 1


async def test_429_waits_for_retry_after_then_succeeds(sleeps):
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, text=review_page(["a", "b"]))

    result = await scraper_for(handler, sleeps).scrape("http://shop.test/p")
    assert len(result.reviews) == 2
    assert 7 in sleeps


async def test_retry_after_is_capped(sleeps):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503, headers={"Retry-After": "86400"})

    result = await scraper_for(handler, sleeps, max_attempts=2).scrape("http://shop.test/p")
    assert result.stopped == "error" and "HTTP 503" in result.error
    assert max(sleeps) == 120


async def test_network_errors_retry_then_give_up(sleeps):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("connection refused")

    result = await scraper_for(handler, sleeps, max_attempts=3).scrape("http://shop.test/p")
    assert result.stopped == "error" and "ConnectError" in result.error
    assert len([s for s in sleeps if s >= 0.5]) == 2  # backoff before attempts 2 and 3


async def test_404_is_not_retried(sleeps):
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(1)
        return httpx.Response(404)

    result = await scraper_for(handler, sleeps).scrape("http://shop.test/p")
    assert result.error.endswith("HTTP 404") and len(calls) == 1


async def test_errors_after_some_pages_keep_what_was_read(sleeps):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.params.get("page") == "2":
            return httpx.Response(500)
        return httpx.Response(200, text=review_page(["a", "b"], "/p?page=2"))

    result = await scraper_for(handler, sleeps, max_attempts=1).scrape("http://shop.test/p")
    assert len(result.reviews) == 2 and result.stopped == "error"


async def test_max_pages_and_next_link_loops(sleeps):
    def endless(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        n = int(request.url.params.get("page", 1))
        return httpx.Response(200, text=review_page([f"r{n}"], f"/p?page={n + 1}"))

    result = await scraper_for(endless, sleeps, max_pages=3).scrape("http://shop.test/p")
    assert result.pages == 3 and result.stopped == "max_pages"

    def loop(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=review_page(["x"], "/p"))

    result = await scraper_for(loop, sleeps).scrape("http://shop.test/p")
    assert result.pages == 1


async def test_sends_an_honest_user_agent_and_throttles(sleeps):
    agents = []

    def handler(request):
        agents.append(request.headers["user-agent"])
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        n = int(request.url.params.get("page", 1))
        return httpx.Response(200, text=review_page([f"r{n}"], f"/p?page={n + 1}" if n < 3 else None))

    await scraper_for(handler, sleeps, request_delay_seconds=1.0, user_agent="TestBot/1.0").scrape("http://shop.test/p")
    assert set(agents) == {"TestBot/1.0"}
    # robots.txt + 3 pages = 4 requests to one host, so at least 3 waits
    assert len([s for s in sleeps if 0 < s <= 1.0]) >= 3


async def test_demo_store_rate_limit_is_survived(store, http, sleeps):
    store.state.block_rate = 0.3
    scraper = Scraper(make_settings(max_attempts=10), client=http, sleep=sleeps, today=TODAY)
    result = await scraper.scrape(f"{STORE}/products/aurora-anc-headphones")
    assert len(result.reviews) == 108 and result.stopped == "last_page"
    assert 1.0 in sleeps  # Retry-After: 1 from the store

def compact_storage(data=None):
    if data is None:
        return None
    return data