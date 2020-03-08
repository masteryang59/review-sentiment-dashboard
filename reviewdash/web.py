"""Dashboard: a read-only JSON API over the SQLite file, plus the static single-page UI."""

import csv
import io
from datetime import date
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import PACKAGE_DIR, Settings, load_themes
from .stats import list_reviews, product_stats

STATIC = PACKAGE_DIR / "static"


def _check_date(value: str | None, name: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, f"{name} must be YYYY-MM-DD") from None


def _cell(value):
    """Reviews are written by strangers: stop spreadsheet apps from running a cell as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def create_app(settings: Settings | None = None, conn=None) -> FastAPI:
    settings = settings or Settings()
    conn = conn or db.connect(settings.db_path)
    themes = load_themes(settings.themes_path)
    app = FastAPI(title="Review Sentiment Dashboard", docs_url="/api/docs", redoc_url=None)
    app.state.conn = conn

    def product_or_404(product_id: int):
        product = db.get_product(conn, product_id)
        if product is None:
            raise HTTPException(404, "no such product")
        return product

    @app.get("/api/health")
    def health():
        return {"ok": True, "analyzer": f"openai:{settings.openai_model}" if settings.use_llm else "lexicon"}

    @app.get("/api/themes")
    def get_themes():
        return [{"key": t.key, "label": t.label, "description": t.description} for t in themes]

    @app.get("/api/products")
    def products():
        return [{k: p[k] for k in ("id", "name", "url", "review_count", "analyzed_count", "lexicon_count",
                                    "avg_rating", "last_scraped_at", "last_scrape_status")}
                for p in db.list_products(conn)]

    @app.get("/api/products/{product_id}/stats")
    def stats(product_id: int, since: str | None = None, until: str | None = None):
        product = product_or_404(product_id)
        data = product_stats(conn, product_id, themes, _check_date(since, "since"), _check_date(until, "until"))
        return data | {"product": {"id": product["id"], "name": product["name"], "url": product["url"],
                                   "last_scraped_at": product["last_scraped_at"]},
                       "summary": db.get_summary(conn, product_id)}

    @app.get("/api/products/{product_id}/reviews")
    def reviews(product_id: int, since: str | None = None, until: str | None = None,
                sentiment: Literal["positive", "neutral", "mixed", "negative"] | None = None,
                theme: str | None = None, theme_sentiment: Literal["positive", "negative"] | None = None,
                mismatch: bool = False, q: str | None = Query(None, max_length=200),
                page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        product_or_404(product_id)
        return list_reviews(conn, product_id, _check_date(since, "since"), _check_date(until, "until"),
                            sentiment, theme, theme_sentiment, mismatch, q, page, page_size)

    @app.get("/api/products/{product_id}/export.csv")
    def export(product_id: int):
        product = product_or_404(product_id)
        data = list_reviews(conn, product_id, page_size=1_000_000)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["published_on", "author", "rating", "title", "body", "sentiment", "score", "summary",
                         "themes", "mismatch", "analyzer", "source_id"])
        for r in data["reviews"]:
            writer.writerow([_cell(v) for v in (
                r["published_on"], r["author"], r["rating"], r["title"], r["body"], r["sentiment"], r["score"],
                r["summary"], "; ".join(f"{t['theme']}:{t['sentiment']}" for t in r["themes"]),
                int(r["mismatch"]), r["analyzer"], r["source_id"],
            )])
        filename = "".join(c if c.isalnum() else "-" for c in product["name"].lower()).strip("-") or "reviews"
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})

    @app.get("/api/runs")
    def runs(limit: int = Query(20, ge=1, le=200)):
        return db.recent_runs(conn, limit)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app

def encrypt_field(data=None):
    if data is None:
        return None
    return data

def update_cache(data=None):
    if data is None:
        return None
    return data