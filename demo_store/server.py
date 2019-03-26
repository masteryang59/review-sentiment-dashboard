"""A fake shop to scrape, so the project can be demoed and tested without touching real sites.

Each product publishes its reviews differently:
  - Aurora headphones: schema.org microdata
  - Ember kettle: JSON-LD in the page head, plain HTML in the body
  - Ridgeline boots: plain HTML with no structured data (needs CSS selectors)
  - Nimbus vacuum: microdata with nested Person authors and text ratings

Reviews are paginated newest-first, 10 per page. A share of requests
(--block-rate) get a 429 with Retry-After, like a rate-limited real shop.
"""

import json
import random
from datetime import date
from html import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .catalog import DemoProduct, DemoReview, add_fresh_reviews, build_catalog

PAGE_SIZE = 10

# The selectors the boots page needs; `seed-demo` saves them with the product.
PLAIN_SELECTORS = {
    "review": "div.rv",
    "id": "@data-review-id",
    "author": ".rv-by",
    "rating": ".rv-stars",
    "title": ".rv-title",
    "body": ".rv-text",
    "date": ".rv-date",
    "next": "a.older",
}

STYLE = """<style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#222}
article,.rv,.card{border-bottom:1px solid #ddd;padding:.8rem 0}
.meta{color:#666;font-size:.9rem}nav a{margin-right:1rem}
</style>"""


def _stars(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)


def _page(title: str, head: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{escape(title)}</title>{STYLE}{head}"
            f"</head><body><p><a href='/'>Demo Store</a></p>{body}</body></html>")


def _microdata(r: DemoReview) -> str:
    return f"""<article itemprop="review" itemscope itemtype="https://schema.org/Review" id="{r.id}">
  <h3 itemprop="name">{escape(r.title)}</h3>
  <div itemprop="reviewRating" itemscope itemtype="https://schema.org/Rating">
    <meta itemprop="ratingValue" content="{r.rating}"><meta itemprop="bestRating" content="5">
    <span aria-hidden="true">{_stars(r.rating)}</span>
  </div>
  <p class="meta">by <span itemprop="author">{escape(r.author)}</span> on
    <time itemprop="datePublished" datetime="{r.published.isoformat()}">{r.published:%B %d, %Y}</time></p>
  <p itemprop="reviewBody">{escape(r.body)}</p>
</article>"""


def _microdata_person(r: DemoReview) -> str:
    return f"""<article itemprop="review" itemscope itemtype="https://schema.org/Review" data-review-id="{r.id}">
  <div itemprop="author" itemscope itemtype="https://schema.org/Person">
    <span itemprop="name">{escape(r.author)}</span> <span class="meta">Verified buyer</span>
  </div>
  <div itemprop="reviewRating" itemscope itemtype="https://schema.org/Rating">
    <span itemprop="ratingValue">{r.rating}</span> out of <span itemprop="bestRating">5</span>
  </div>
  <strong itemprop="headline">{escape(r.title)}</strong>
  <meta itemprop="datePublished" content="{r.published.isoformat()}">
  <div itemprop="description">{escape(r.body)}</div>
</article>"""


def _plain_card(r: DemoReview) -> str:
    return f"""<div class="card"><b>{escape(r.title)}</b> {_stars(r.rating)}
  <div class="meta">{escape(r.author)} · {r.published:%d %b %Y}</div><p>{escape(r.body)}</p></div>"""


def _selector_card(r: DemoReview) -> str:
    return f"""<div class="rv" data-review-id="{r.id}">
  <span class="rv-stars" title="{r.rating} out of 5">{_stars(r.rating)}</span> <b class="rv-title">{escape(r.title)}</b>
  <div class="meta"><span class="rv-by">by {escape(r.author)}</span> · <span class="rv-date">{r.published:%B %d, %Y}</span></div>
  <div class="rv-text">{escape(r.body)}</div>
</div>"""


def _json_ld(product: DemoProduct, reviews: list[DemoReview]) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.name,
        "description": product.blurb,
        "offers": {"@type": "Offer", "price": product.price.lstrip("$"), "priceCurrency": "USD"},
        "review": [
            {
                "@type": "Review",
                "identifier": r.id,
                "name": r.title,
                "author": {"@type": "Person", "name": r.author},
                "datePublished": r.published.isoformat(),
                "reviewRating": {"@type": "Rating", "ratingValue": r.rating, "bestRating": 5},
                "reviewBody": r.body,
            }
            for r in reviews
        ],
    }
    # Escape "<" so a review containing "</script>" can't break out of the tag.
    payload = json.dumps(data).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{payload}</script>'


def render_product_page(product: DemoProduct, page: int) -> str:
    pages = max(1, -(-len(product.reviews) // PAGE_SIZE))
    if page < 1 or page > pages:
        raise HTTPException(404, "no such page")
    reviews = product.reviews[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    base = f"/products/{product.slug}"
    head, nav = "", ""
    if page > 1:
        head += f'<link rel="prev" href="{base}?page={page - 1}">'
    if page < pages:
        head += f'<link rel="next" href="{base}?page={page + 1}">'

    if product.markup == "plain":
        cards = "\n".join(_selector_card(r) for r in reviews)
        older = f'<a class="older" href="{base}?page={page + 1}">Older reviews →</a>' if page < pages else ""
        head = ""  # no rel links either: the "next" selector has to find the button
        nav = older
        body = f"<h1>{escape(product.name)}</h1><p>{escape(product.blurb)} {product.price}</p><h2>Reviews</h2>{cards}"
        return _page(product.name, head, body + f"<nav>{nav}</nav>")

    if product.markup == "json-ld":
        head += _json_ld(product, reviews)
        cards = "\n".join(_plain_card(r) for r in reviews)
        body = f"<h1>{escape(product.name)}</h1><p>{escape(product.blurb)} {product.price}</p><h2>Reviews</h2>{cards}"
    else:
        render = _microdata if product.markup == "microdata" else _microdata_person
        cards = "\n".join(render(r) for r in reviews)
        body = (f'<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">{escape(product.name)}</h1>'
                f'<p itemprop="description">{escape(product.blurb)}</p><p>{product.price}</p>'
                f"<h2>Reviews</h2>{cards}</div>")
    if page > 1:
        nav += f'<a rel="prev" href="{base}?page={page - 1}">← Newer</a>'
    if page < pages:
        nav += f'<a rel="next" href="{base}?page={page + 1}">Older →</a>'
    return _page(product.name, head, body + f"<nav>{nav}</nav>")


def create_app(today: date | None = None, block_rate: float = 0.0, seed: int | None = None) -> FastAPI:
    app = FastAPI(title="Demo Store", docs_url=None, redoc_url=None)
    app.state.catalog = build_catalog(today)
    app.state.block_rate = block_rate
    app.state.requests = []
    rng = random.Random(seed)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        path = request.url.path
        app.state.requests.append({"path": path, "query": request.url.query,
                                   "user_agent": request.headers.get("user-agent", "")})
        del app.state.requests[:-200]
        if path.startswith("/products/") and rng.random() < app.state.block_rate:
            return PlainTextResponse("Too many requests", status_code=429, headers={"Retry-After": "1"})
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        items = "".join(
            f'<li><a href="/products/{p.slug}">{escape(p.name)}</a> · {len(p.reviews)} reviews · {p.markup}</li>'
            for p in app.state.catalog.values()
        )
        return _page("Demo Store", "", f"<h1>Demo Store</h1><ul>{items}</ul>"
                     "<p class=meta>Made-up products and reviews for review-sentiment-dashboard.</p>")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /admin\nDisallow: /cart\n"

    @app.get("/products/{slug}", response_class=HTMLResponse)
    def product_page(slug: str, page: int = 1):
        product = app.state.catalog.get(slug)
        if product is None:
            raise HTTPException(404, "no such product")
        return render_product_page(product, page)

    @app.post("/admin/new-reviews")
    def new_reviews(slug: str, count: int = 3):
        product = app.state.catalog.get(slug)
        if product is None:
            raise HTTPException(404, "no such product")
        fresh = add_fresh_reviews(product, max(1, min(count, 50)))
        return {"added": [r.id for r in fresh], "total": len(product.reviews)}

    @app.get("/admin/truth.json")
    def truth():
        """Ground truth for scripts/evaluate.py."""
        return JSONResponse({
            p.slug: {
                "name": p.name,
                "selectors": PLAIN_SELECTORS if p.markup == "plain" else None,
                "reviews": {
                    r.id: {"sentiment": r.sentiment, "rating": r.rating,
                           "themes": {k: sorted(v) for k, v in r.themes.items()}}
                    for r in p.reviews
                },
            }
            for p in app.state.catalog.values()
        })

    @app.get("/admin/requests")
    def recent_requests():
        return app.state.requests[-50:]

    return app

def generate_report(data=None):
    if data is None:
        return None
    return data

def report_error(data=None):
    if data is None:
        return None
    return data