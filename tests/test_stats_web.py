import csv
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient

from reviewdash import db
from reviewdash.extract import ScrapedReview
from reviewdash.models import ReviewAnalysis, ThemeMention
from reviewdash.stats import list_reviews, product_stats, summary_facts
from reviewdash.summary import template_summary
from reviewdash.web import create_app

from .conftest import make_settings


def seed(conn, rows):
    """rows: (source_id, rating, published, body, sentiment, [(theme, polarity, evidence)])"""
    product_id = db.add_product(conn, "Widget", "https://shop.test/widget")
    db.upsert_reviews(conn, product_id, [
        ScrapedReview(source_id=sid, body=body, rating=rating, published_on=published, author="A", title=None)
        for sid, rating, published, body, _, _ in rows
    ])
    for sid, _, _, _, sentiment, themes in rows:
        if sentiment is None:
            continue
        review = conn.execute("SELECT * FROM reviews WHERE source_id = ?", (sid,)).fetchone()
        analysis = ReviewAnalysis(id=sid, sentiment=sentiment, score={"positive": 0.8, "negative": -0.8}.get(sentiment, 0),
                                  themes=[ThemeMention(theme=t, sentiment=p, evidence=e) for t, p, e in themes],
                                  summary="s")
        db.save_analysis(conn, review, analysis, "openai:test", "v1")
    conn.commit()
    return product_id


ROWS = [
    ("r1", 5, date(2026, 1, 10), "Battery is great.", "positive", [("battery", "positive", "Battery is great")]),
    ("r2", 5, date(2026, 1, 20), "Battery died. =cmd|' /C calc'!A0", "negative", [("battery", "negative", "Battery died")]),
    ("r3", 1, date(2026, 3, 5), "Love the design, 100% happy_face", "positive", [("design", "positive", "")]),
    ("r4", 3, date(2026, 3, 9), "It is fine but slow app.", "mixed",
     [("connectivity", "negative", "slow app"), ("design", "positive", "")]),
    ("r5", 2, None, "No date on this one.", None, []),
]


@pytest.fixture
def seeded(conn):
    return seed(conn, ROWS)


def test_product_stats(conn, themes, seeded):
    stats = product_stats(conn, seeded, themes)
    totals = stats["totals"]
    assert (totals["reviews"], totals["analyzed"], totals["positive"], totals["negative"], totals["mixed"]) == (5, 4, 2, 1, 1)
    assert totals["net_sentiment"] == 25.0  # (2 - 1) / 4
    assert totals["mismatches"] == 2  # r2: 5 stars but negative; r3: 1 star but positive
    assert totals["avg_rating"] == 3.2
    # Months without reviews are filled in, undated reviews are left out.
    assert [m["month"] for m in stats["monthly"]] == ["2026-01", "2026-02", "2026-03"]
    assert stats["monthly"][1]["total"] == 0
    assert stats["monthly"][2]["mixed"] == 1
    # Sorted by total mentions, ties by key.
    assert [(t["theme"], t["positive"], t["negative"]) for t in stats["themes"]] == [
        ("battery", 1, 1), ("design", 2, 0), ("connectivity", 0, 1)]
    assert stats["themes"][1]["label"] == "Design & looks"
    assert stats["analyzers"] == {"openai:test": 4}
    assert stats["range"]["first_review"] == "2026-01-10"


def test_date_range_and_previous_period(conn, themes, seeded):
    stats = product_stats(conn, seeded, themes, since="2026-03-01", until="2026-03-31")
    assert stats["totals"]["reviews"] == 2
    previous = stats["previous"]
    assert (previous["since"], previous["until"]) == ("2026-01-29", "2026-02-28")
    assert previous["reviews"] == 0
    assert [m["month"] for m in stats["monthly"]] == ["2026-03"]


def test_list_reviews_filters(conn, seeded):
    ids = lambda **kw: [r["source_id"] for r in list_reviews(conn, seeded, **kw)["reviews"]]
    assert ids() == ["r4", "r3", "r2", "r1", "r5"]  # newest first, undated last
    assert ids(sentiment="positive") == ["r3", "r1"]
    assert ids(theme="battery") == ["r2", "r1"]
    assert ids(theme="battery", theme_sentiment="negative") == ["r2"]
    assert ids(mismatch=True) == ["r3", "r2"]
    assert ids(q="100%") == ["r3"]  # LIKE wildcards are escaped
    assert ids(q="happy_") == ["r3"]
    assert ids(q="%") == ["r3"]
    assert ids(since="2026-03-01") == ["r4", "r3"]
    page = list_reviews(conn, seeded, page=2, page_size=2)
    assert page["total"] == 5 and [r["source_id"] for r in page["reviews"]] == ["r2", "r1"]
    r4 = list_reviews(conn, seeded, q="slow")["reviews"][0]
    assert {t["theme"] for t in r4["themes"]} == {"connectivity", "design"}


def test_summary_facts_and_template(conn, themes, seeded):
    facts = summary_facts(conn, seeded, themes)
    assert facts["sentiment_counts"] == {"positive": 2, "neutral": 0, "mixed": 1, "negative": 1}
    assert facts["quotes"]["Battery life (negative)"] == ["Battery died"]
    summary = template_summary("Widget", facts)
    assert summary.headline.startswith("50% of 4 reviews are positive and 25% negative")
    assert any("contradicts" in issue for issue in summary.issues)
    assert template_summary("Widget", facts | {"sentiment_counts": dict.fromkeys(facts["sentiment_counts"], 0)}).headline \
        == "No analyzed reviews for Widget yet."


def test_rising_complaints_are_detected(conn, themes):
    rows = [(f"old{i}", 4, date(2025, 12, 1), "ok", "negative", [("battery", "negative", "")]) for i in range(1)]
    rows += [(f"new{i}", 2, date(2026, 8, 1 + i), "bad", "negative", [("battery", "negative", "")]) for i in range(6)]
    product_id = seed(conn, rows)
    [trend] = summary_facts(conn, product_id, themes)["trends_last_90_days"]
    assert trend == {"theme": "Battery life", "sentiment": "negative", "last_90_days": 6, "previous_90_days": 0}


@pytest.fixture
def client(conn, seeded):
    with TestClient(create_app(make_settings(), conn=conn)) as test_client:
        yield test_client


def test_api(client, seeded):
    assert client.get("/api/health").json() == {"ok": True, "analyzer": "lexicon"}
    assert len(client.get("/api/themes").json()) == 10
    [product] = client.get("/api/products").json()
    assert (product["name"], product["review_count"], product["analyzed_count"]) == ("Widget", 5, 4)
    stats = client.get(f"/api/products/{seeded}/stats", params={"since": "2026-03-01"}).json()
    assert stats["totals"]["reviews"] == 2 and stats["product"]["name"] == "Widget" and stats["summary"] is None
    reviews = client.get(f"/api/products/{seeded}/reviews", params={"theme": "battery", "page_size": 1}).json()
    assert reviews["total"] == 2 and len(reviews["reviews"]) == 1


@pytest.mark.parametrize("path, params, status", [
    ("/api/products/999/stats", {}, 404),
    ("/api/products/999/reviews", {}, 404),
    ("/api/products/{id}/stats", {"since": "March"}, 422),
    ("/api/products/{id}/reviews", {"sentiment": "angry"}, 422),
    ("/api/products/{id}/reviews", {"page_size": 1000}, 422),
])
def test_api_errors(client, seeded, path, params, status):
    assert client.get(path.format(id=seeded), params=params).status_code == status


def test_csv_export_neutralizes_formulas(client, seeded):
    response = client.get(f"/api/products/{seeded}/export.csv")
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="widget.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 5
    r2 = next(r for r in rows if r["source_id"] == "r2")
    assert r2["themes"] == "battery:negative" and r2["mismatch"] == "1"
    assert all(not value.startswith(("=", "+", "@")) for row in rows for value in row.values())


def test_dashboard_page_and_assets(client):
    page = client.get("/")
    assert page.status_code == 200 and "Review Sentiment" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
