import json

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from demo_store.catalog import DemoProduct, DemoReview, build_catalog
from demo_store.server import create_app, render_product_page

from .conftest import TODAY


def test_catalog_is_deterministic_and_newest_first():
    a, b = build_catalog(TODAY), build_catalog(TODAY)
    for slug in a:
        assert [(r.id, r.body, r.rating, r.published) for r in a[slug].reviews] == \
               [(r.id, r.body, r.rating, r.published) for r in b[slug].reviews]
        dates = [r.published for r in a[slug].reviews]
        assert dates == sorted(dates, reverse=True)
        assert all(1 <= r.rating <= 5 for r in a[slug].reviews)


def test_story_arcs_are_in_the_data():
    headphones = build_catalog(TODAY)["aurora-anc-headphones"].reviews

    def battery_complaints(lo, hi):
        return sum("negative" in r.themes.get("battery", set())
                   for r in headphones if lo <= (TODAY - r.published).days < hi)

    assert battery_complaints(0, 80) >= 3 * max(1, battery_complaints(80, 365))


def test_ground_truth_labels_match_themes():
    for product in build_catalog(TODAY).values():
        for review in product.reviews:
            polarities = set().union(*review.themes.values()) if review.themes else set()
            if review.sentiment == "positive":
                assert polarities == {"positive"}
            if review.sentiment == "negative":
                assert polarities == {"negative"}


def test_pages_robots_truth_and_new_reviews():
    app = create_app(today=TODAY)
    client = TestClient(app)
    assert "Disallow: /admin" in client.get("/robots.txt").text
    assert client.get("/products/nope").status_code == 404
    assert client.get("/products/ember-gooseneck-kettle?page=99").status_code == 404
    truth = client.get("/admin/truth.json").json()
    assert set(truth) == set(app.state.catalog)
    assert truth["ridgeline-trail-boots"]["selectors"]["review"] == "div.rv"
    before = len(app.state.catalog["nimbus-robot-vacuum"].reviews)
    added = client.post("/admin/new-reviews", params={"slug": "nimbus-robot-vacuum", "count": 2}).json()
    assert added["total"] == before + 2
    assert app.state.catalog["nimbus-robot-vacuum"].reviews[0].id in added["added"]


def test_block_rate_returns_429_with_retry_after():
    client = TestClient(create_app(today=TODAY, block_rate=1.0))
    response = client.get("/products/aurora-anc-headphones")
    assert response.status_code == 429 and response.headers["retry-after"] == "1"
    assert client.get("/robots.txt").status_code == 200  # only product pages are limited


def test_json_ld_cannot_be_broken_out_of():
    review = DemoReview("x1", "Eve", 1, "</script><script>alert(1)</script>", "Body </script>", TODAY, "neutral")
    product = DemoProduct("p", "P", "thing", "$1", "json-ld", 1, "blurb", [review])
    soup = BeautifulSoup(render_product_page(product, 1), "html.parser")
    [script] = soup.find_all("script")
    data = json.loads(script.string)
    assert data["review"][0]["name"] == "</script><script>alert(1)</script>"

def warm_cache(data=None):
    if data is None:
        return None
    return data

def warm_cache(data=None):
    if data is None:
        return None
    return data