from datetime import date

import pytest

from reviewdash.extract import fallback_id, parse_date, parse_page, parse_rating, validate_selectors

TODAY = date(2026, 10, 29)


@pytest.mark.parametrize("value, best, expected", [
    (4, None, 4.0),
    ("4", None, 4.0),
    ("4.5 out of 5 stars", None, 4.5),
    ("8/10", None, 4.0),
    ("Rated 3 of 5", None, 3.0),
    ("★★★★☆", None, 4.0),
    ("★★★", None, 3.0),  # no empty stars drawn: assume a 5-star scale
    ("80", "100", 4.0),
    ("4,5", None, 4.5),
    ("7", None, None),  # above the 5-point scale
    ("great", None, None),
    (None, None, None),
    ("", None, None),
    ("3", "not-a-number", 3.0),
])
def test_parse_rating(value, best, expected):
    assert parse_rating(value, best) == expected


@pytest.mark.parametrize("value, expected", [
    ("2026-03-02", date(2026, 3, 2)),
    ("2026-03-02T10:15:00Z", date(2026, 3, 2)),
    ("March 2, 2026", date(2026, 3, 2)),
    ("Mar 2nd, 2026", date(2026, 3, 2)),
    ("2 March 2026", date(2026, 3, 2)),
    ("Reviewed on March 2, 2026", date(2026, 3, 2)),
    ("Reviewed in the United States on March 2, 2026", date(2026, 3, 2)),
    ("02.03.2026", date(2026, 3, 2)),
    ("today", TODAY),
    ("yesterday", date(2026, 10, 28)),
    ("3 days ago", date(2026, 10, 26)),
    ("a week ago", date(2026, 10, 22)),
    ("2 months ago", date(2026, 8, 30)),
    ("sometime", None),
    ("", None),
])
def test_parse_date(value, expected):
    assert parse_date(value, TODAY) == expected


JSON_LD_PAGE = """<html><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
  {"@type": "Product", "name": "Test Kettle", "review": [
    {"@type": "Review", "identifier": "r1", "name": "Great", "author": {"@type": "Person", "name": "Ann"},
     "datePublished": "2026-05-01", "reviewRating": {"ratingValue": "9", "bestRating": "10"},
     "reviewBody": "Pours  beautifully.\\n Fast boil."},
    {"@type": "Review", "author": "Bob", "reviewBody": "No id here", "datePublished": "2026-05-02"},
    {"@type": "Review", "author": "Empty", "reviewBody": "   "}
  ]}
]}</script>
<script type="application/ld+json">{ not json</script>
<link rel="next" href="/kettle?page=2">
</head><body><h1>Ignored heading</h1></body></html>"""


def test_json_ld_with_graph_and_normalization():
    page = parse_page(JSON_LD_PAGE, "https://shop.test/kettle")
    assert page.method == "json-ld"
    assert page.product_name == "Test Kettle"
    assert page.next_url == "https://shop.test/kettle?page=2"
    first, second = page.reviews  # the blank review is skipped
    assert (first.source_id, first.author, first.rating, first.title) == ("r1", "Ann", 4.5, "Great")
    assert first.body == "Pours beautifully. Fast boil."
    assert first.published_on == date(2026, 5, 1)
    assert second.source_id == fallback_id("Bob", date(2026, 5, 2), "No id here")
    assert second.rating is None


MICRODATA_PAGE = """<div itemscope itemtype="https://schema.org/Product">
<h1 itemprop="name">Phones</h1>
<div itemprop="review" itemscope itemtype="https://schema.org/Review" id="rv-9">
  <span itemprop="name">Nice</span>
  <div itemprop="author" itemscope itemtype="https://schema.org/Person"><span itemprop="name">Cleo</span></div>
  <div itemprop="reviewRating" itemscope itemtype="https://schema.org/Rating">
    <span itemprop="ratingValue">3</span>/<span itemprop="bestRating">5</span></div>
  <time itemprop="datePublished" datetime="2026-04-04">April 4</time>
  <p itemprop="reviewBody">Good sound.</p>
</div></div>
<a rel="next nofollow" href="?page=3">Next</a>"""


def test_microdata_scopes_props_to_their_item():
    page = parse_page(MICRODATA_PAGE, "https://shop.test/p?page=2")
    assert page.method == "microdata"
    # The Product's "name" must not leak into the review title, nor the Person's name.
    assert page.product_name == "Phones"
    [review] = page.reviews
    assert (review.source_id, review.title, review.author, review.rating) == ("rv-9", "Nice", "Cleo", 3.0)
    assert review.published_on == date(2026, 4, 4)
    assert page.next_url == "https://shop.test/p?page=3"


SELECTOR_PAGE = """<h1>Boots</h1>
<div class="r" data-id="a1"><span class="s" title="2 out of 5">★★☆☆☆</span><b>Meh</b>
  <span class="by">by Dan</span><span class="d">3 days ago</span><div class="t">Tight fit.</div></div>
<div class="r" data-id="a1"><div class="t">Duplicate of a1</div></div>
<div class="r"><div class="t"></div></div>
<a class="more" href="/boots/2">More</a>"""

SELECTORS = {"review": "div.r", "id": "@data-id", "rating": ".s@title", "title": "b", "author": ".by",
             "date": ".d", "body": ".t", "next": "a.more"}


def test_selectors_strategy_and_dedup():
    page = parse_page(SELECTOR_PAGE, "https://shop.test/boots", SELECTORS, today=TODAY)
    assert page.method == "selectors"
    assert page.product_name == "Boots"
    assert page.next_url == "https://shop.test/boots/2"
    [review] = page.reviews
    assert (review.source_id, review.author, review.rating, review.title) == ("a1", "Dan", 2.0, "Meh")
    assert review.published_on == date(2026, 10, 26)


def test_selectors_that_match_nothing_fall_back_to_structured_data():
    page = parse_page(JSON_LD_PAGE, "https://shop.test/kettle", {"review": ".nope", "body": ".t"})
    assert page.method == "json-ld"
    assert len(page.reviews) == 2


def test_page_without_reviews():
    page = parse_page("<html><title>Empty shop</title></html>", "https://shop.test/")
    assert page.reviews == [] and page.method is None and page.next_url is None
    assert page.product_name == "Empty shop"


def test_validate_selectors():
    assert validate_selectors(SELECTORS) == SELECTORS
    with pytest.raises(ValueError, match="unknown"):
        validate_selectors({"review": "x", "body": "y", "stars": "z"})
    with pytest.raises(ValueError, match="at least"):
        validate_selectors({"review": "x"})

def evict_stale(data=None):
    if data is None:
        return None
    return data

def evict_stale(data=None):
    if data is None:
        return None
    return data