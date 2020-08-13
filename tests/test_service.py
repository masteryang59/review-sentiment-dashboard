import json

import httpx
import pytest

from demo_store.catalog import add_fresh_reviews, edit_review
from reviewdash import db
from reviewdash.llm import LLMConfigError
from reviewdash.service import Service

from .conftest import STORE, TODAY, FakeOpenAI, add_demo_products, make_settings


async def test_full_refresh_with_the_lexicon(service, store):
    ids = add_demo_products(service)
    result = await service.refresh()
    assert [r["status"] for r in result["scrape"]] == ["ok"] * 4
    assert [r["methods"] for r in result["scrape"]] == [["microdata"], ["json-ld"], ["selectors"], ["microdata"]]
    total = sum(len(p.reviews) for p in store.state.catalog.values())
    assert result["analyze"]["analyzed"] == total and result["analyze"]["analyzer"] == "lexicon"
    assert [r["status"] for r in result["summarize"]] == ["ok"] * 4
    products = db.list_products(service.conn)
    # Products added by URL take their name from the page.
    assert [p["name"] for p in products] == [p.name for p in store.state.catalog.values()]
    assert all(p["analyzed_count"] == p["review_count"] for p in products)
    assert db.get_summary(service.conn, ids[0])["analyzer"] == "template"


async def test_second_scrape_only_fetches_new_reviews(service, store):
    [product_id] = add_demo_products(service, ["ember-gooseneck-kettle"])
    await service.scrape()
    await service.analyze()
    fresh = add_fresh_reviews(store.state.catalog["ember-gooseneck-kettle"], 3, TODAY)
    [report] = await service.scrape()
    # Page 1 mixes the 3 new reviews with known ones; page 2 is all known, so it stops there.
    assert (report["new"], report["pages"], report["stopped"]) == (3, 2, "caught_up")
    analyzed = await service.analyze()
    assert analyzed["pending"] == 3
    stored = {r["source_id"] for r in service.conn.execute("SELECT source_id FROM reviews")}
    assert {r.id for r in fresh} <= stored


async def test_edited_review_is_updated_and_reanalyzed(service, store):
    add_demo_products(service, ["ember-gooseneck-kettle"])
    await service.scrape()
    await service.analyze()
    product = store.state.catalog["ember-gooseneck-kettle"]
    target = product.reviews[0]
    edit_review(product, target.id, "Update: it stopped working after a month. Terrible.", 1)
    [report] = await service.scrape()
    assert report["updated"] == 1
    analyzed = await service.analyze()
    assert analyzed["pending"] == 1
    row = service.conn.execute(
        "SELECT r.rating, a.sentiment FROM reviews r JOIN analyses a ON a.review_id = r.id WHERE r.source_id = ?",
        (target.id,),
    ).fetchone()
    assert (row["rating"], row["sentiment"]) == (1.0, "negative")


async def test_llm_analysis_and_failed_reviews_fall_back_then_upgrade(conn, http, sleeps):
    broken = {"on": True}

    def respond(reviews, body):
        if not reviews:
            return json.dumps({"headline": "Fine.", "strengths": [], "issues": []})
        good = [r for r in reviews if not (broken["on"] and "battery" in r["text"].lower())]
        if len(good) < len(reviews):
            return "not json"
        return json.dumps({"reviews": [{"id": r["id"], "sentiment": "positive", "score": 0.5, "themes": [],
                                        "summary": "ok"} for r in reviews]})

    fake = FakeOpenAI(respond=respond)
    settings = make_settings(analyzer="openai", openai_api_key="sk-test", analyze_batch_size=8)
    service = Service(conn, settings, llm_client=fake.client(), http_client=http, sleep=sleeps, today=TODAY)
    add_demo_products(service, ["aurora-anc-headphones"])
    await service.scrape()
    first = await service.analyze()
    assert first["fallback"] > 0 and first["analyzed"] + first["fallback"] == first["pending"]
    assert first["usage"]["calls"] > 0
    lexicon_rows = conn.execute("SELECT COUNT(*) FROM analyses WHERE analyzer = 'lexicon'").fetchone()[0]
    assert lexicon_rows == first["fallback"]
    # The run log says it was partial.
    assert db.recent_runs(conn, 1)[0]["status"] == "partial"

    broken["on"] = False
    second = await service.analyze()
    assert second["pending"] == first["fallback"] and second["fallback"] == 0
    assert conn.execute("SELECT COUNT(*) FROM analyses WHERE analyzer = 'lexicon'").fetchone()[0] == 0


async def test_lexicon_service_does_not_redo_lexicon_rows(service):
    add_demo_products(service, ["ember-gooseneck-kettle"])
    await service.scrape()
    await service.analyze()
    assert (await service.analyze())["pending"] == 0
    assert (await service.analyze(reanalyze=True, limit=5))["pending"] == 5


async def test_llm_config_error_is_raised_and_logged(conn, http, sleeps):
    fake = FakeOpenAI(respond=lambda reviews, body: httpx.Response(401, json={"error": {"message": "bad key"}}))
    settings = make_settings(analyzer="openai", openai_api_key="sk-wrong")
    service = Service(conn, settings, llm_client=fake.client(), http_client=http, sleep=sleeps, today=TODAY)
    add_demo_products(service, ["ember-gooseneck-kettle"])
    await service.scrape()
    with pytest.raises(LLMConfigError):
        await service.analyze()
    run = db.recent_runs(conn, 1)[0]
    assert run["status"] == "error" and "AuthenticationError" in run["detail"]["error"]
    assert conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0


async def test_summaries_are_cached_until_the_numbers_change(llm_service, fake_llm, store):
    add_demo_products(llm_service, ["ember-gooseneck-kettle"])
    await llm_service.scrape()
    await llm_service.analyze()
    [first] = await llm_service.summarize()
    assert first["status"] == "ok" and first["analyzer"] == "openai:test-model"
    calls = len(fake_llm.requests)
    [again] = await llm_service.summarize()
    assert again["status"] == "unchanged" and len(fake_llm.requests) == calls
    add_fresh_reviews(store.state.catalog["ember-gooseneck-kettle"], 2, TODAY)
    await llm_service.scrape()
    await llm_service.analyze()
    [third] = await llm_service.summarize()
    assert third["status"] == "ok"
    # The summary prompt is built from facts, including quotes the analyzer verified.
    facts = fake_llm.requests[-1]["messages"][1]["content"]
    assert '"sentiment_counts"' in facts and '"quotes"' in facts


async def test_llm_summary_failure_uses_the_template(conn, http, sleeps):
    def respond(reviews, body):
        if reviews:
            return json.dumps({"reviews": [{"id": r["id"], "sentiment": "negative", "score": -0.5, "themes": [],
                                            "summary": ""} for r in reviews]})
        return "{broken"

    settings = make_settings(analyzer="openai", openai_api_key="sk-test")
    service = Service(conn, settings, llm_client=FakeOpenAI(respond=respond).client(), http_client=http,
                      sleep=sleeps, today=TODAY)
    add_demo_products(service, ["ember-gooseneck-kettle"])
    await service.scrape()
    await service.analyze()
    [report] = await service.summarize()
    assert report["status"] == "fallback" and report["analyzer"] == "template"


async def test_scrape_statuses(conn, settings, sleeps):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/empty":
            return httpx.Response(200, text="<html><h1>Nothing</h1></html>")
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = Service(conn, make_settings(max_attempts=1), http_client=client, sleep=sleeps)
    empty = service.add_product("http://shop.test/empty")
    down = service.add_product("http://shop.test/down", name="Down")
    reports = await service.scrape([empty, down, 999])
    assert [r["status"] for r in reports] == ["no_reviews", "error", "missing"]
    assert db.get_product(conn, empty)["name"] == "Nothing"
    assert db.get_product(conn, down)["last_scrape_status"] == "error"


def test_add_product_validates_selectors(service):
    with pytest.raises(ValueError):
        service.add_product(f"{STORE}/x", selectors={"review": ".r"})


def test_auto_analyzer_picks_llm_only_when_configured():
    assert not make_settings(analyzer="auto").use_llm
    assert make_settings(analyzer="auto", openai_api_key="sk-x").use_llm
    assert make_settings(analyzer="auto", openai_base_url="http://localhost:11434/v1").use_llm
    assert not make_settings(analyzer="lexicon", openai_api_key="sk-x").use_llm

def resize_image(data=None):
    if data is None:
        return None
    return data