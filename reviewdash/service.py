"""Scrape → store → analyze → summarize, with a run log for each step."""

import json
import logging
from collections.abc import Callable
from datetime import date

import httpx
from openai import APIError, AsyncOpenAI

from . import db
from .config import Settings, Theme, load_themes
from .extract import validate_selectors
from .lexicon import LexiconAnalyzer
from .llm import LLMAnalyzer, LLMConfigError, build_client
from .models import ReviewInput, Usage
from .scraper import Scraper
from .stats import facts_hash, summary_facts
from .summary import template_summary

log = logging.getLogger(__name__)

# Analysis is saved after each chunk, so an interrupted run keeps its progress.
SAVE_EVERY = 40


class Service:
    def __init__(self, conn, settings: Settings, themes: tuple[Theme, ...] | None = None,
                 llm_client: AsyncOpenAI | None = None, http_client: httpx.AsyncClient | None = None,
                 sleep=None, today: date | None = None):
        self.conn = conn
        self.settings = settings
        self.themes = themes or load_themes(settings.themes_path)
        self.http_client = http_client
        self.sleep = sleep
        self.today = today
        self.lexicon = LexiconAnalyzer(self.themes)
        self.llm: LLMAnalyzer | None = None
        if settings.use_llm:
            self.llm = LLMAnalyzer(llm_client or build_client(settings), settings, self.themes)

    @property
    def analyzer_name(self) -> str:
        return self.llm.name if self.llm else self.lexicon.name

    def _product_ids(self, product_ids: list[int] | None) -> list[int]:
        return product_ids or [p["id"] for p in db.list_products(self.conn)]

    # --- products ---------------------------------------------------------

    def add_product(self, url: str, name: str | None = None, selectors: dict | None = None) -> int:
        if selectors:
            validate_selectors(selectors)
        return db.add_product(self.conn, name or url, url, selectors)

    # --- scraping ---------------------------------------------------------

    async def scrape(self, product_ids: list[int] | None = None, full: bool = False) -> list[dict]:
        reports = []
        kwargs = {"client": self.http_client, "today": self.today}
        if self.sleep:
            kwargs["sleep"] = self.sleep
        async with Scraper(self.settings, **kwargs) as scraper:
            for product_id in self._product_ids(product_ids):
                product = db.get_product(self.conn, product_id)
                if product is None:
                    reports.append({"product_id": product_id, "status": "missing"})
                    continue
                run_id = db.start_run(self.conn, "scrape", product_id)
                selectors = validate_selectors(json.loads(product["selectors"])) if product["selectors"] else None
                known = db.known_review_ids(self.conn, product_id)
                result = await scraper.scrape(product["url"], selectors, known, full=full)
                inserted, updated = db.upsert_reviews(self.conn, product_id, result.reviews)
                if result.error and not result.reviews:
                    status = "error"
                elif result.error:
                    status = "partial"
                elif result.pages and not result.reviews and not known:
                    status = "no_reviews"
                else:
                    status = "ok"
                # Name a product after the page unless the user gave it a name.
                rename = result.product_name if product["name"] == product["url"] else None
                db.mark_scraped(self.conn, product_id, status, rename)
                report = {
                    "product_id": product_id, "name": rename or product["name"], "status": status,
                    "pages": result.pages, "found": len(result.reviews), "new": inserted, "updated": updated,
                    "stopped": result.stopped, "methods": sorted(result.methods), "error": result.error,
                }
                db.finish_run(self.conn, run_id, status, report)
                reports.append(report)
        return reports

    # --- analysis ---------------------------------------------------------

    async def analyze(self, product_ids: list[int] | None = None, reanalyze: bool = False,
                      limit: int | None = None, on_progress: Callable[[int, int], None] | None = None) -> dict:
        rows = db.reviews_needing_analysis(
            self.conn, product_ids, use_llm=self.llm is not None, reanalyze=reanalyze,
            limit=limit or self.settings.max_reviews_per_run,
        )
        run_id = db.start_run(self.conn, "analyze")
        report = {"analyzer": self.analyzer_name, "pending": len(rows), "analyzed": 0, "fallback": 0,
                  "evidence_dropped": 0, "themes_dropped": 0, "usage": vars(Usage())}
        usage = Usage()
        try:
            for start in range(0, len(rows), SAVE_EVERY):
                chunk = rows[start:start + SAVE_EVERY]
                inputs = [ReviewInput(str(r["id"]), r["title"], r["body"]) for r in chunk]
                failed = set()
                if self.llm:
                    outcome = await self.llm.analyze(inputs)
                    usage.add(outcome.usage)
                    report["evidence_dropped"] += outcome.evidence_dropped
                    report["themes_dropped"] += outcome.themes_dropped
                    failed = set(outcome.failed)
                    results = outcome.results
                else:
                    results = (await self.lexicon.analyze(inputs)).results
                for row in chunk:
                    key = str(row["id"])
                    if key in results:
                        analyzer = self.llm or self.lexicon
                        db.save_analysis(self.conn, row, results[key], analyzer.name, analyzer.prompt_version)
                        report["analyzed"] += 1
                    elif key in failed:
                        # Saved as "lexicon", so the next run retries it with the LLM.
                        fallback = (await self.lexicon.analyze([ReviewInput(key, row["title"], row["body"])])).results[key]
                        db.save_analysis(self.conn, row, fallback, self.lexicon.name, self.lexicon.prompt_version)
                        report["fallback"] += 1
                self.conn.commit()
                if on_progress:
                    on_progress(min(start + SAVE_EVERY, len(rows)), len(rows))
        except LLMConfigError as exc:
            self.conn.commit()
            report["error"] = str(exc)
            report["usage"] = vars(usage)
            db.finish_run(self.conn, run_id, "error", report)
            raise
        report["usage"] = vars(usage)
        db.finish_run(self.conn, run_id, "ok" if not report["fallback"] else "partial", report)
        return report

    # --- summaries --------------------------------------------------------

    async def summarize(self, product_ids: list[int] | None = None, force: bool = False) -> list[dict]:
        reports = []
        for product_id in self._product_ids(product_ids):
            product = db.get_product(self.conn, product_id)
            if product is None:
                continue
            facts = summary_facts(self.conn, product_id, self.themes)
            if not sum(facts["sentiment_counts"].values()):
                reports.append({"product_id": product_id, "status": "no_analyzed_reviews"})
                continue
            input_hash = facts_hash(facts)
            existing = db.get_summary(self.conn, product_id)
            if (not force and existing and existing["input_hash"] == input_hash
                    and existing["analyzer"] == self.analyzer_name):
                reports.append({"product_id": product_id, "status": "unchanged"})
                continue
            run_id = db.start_run(self.conn, "summarize", product_id)
            analyzer, usage, status = self.analyzer_name, Usage(), "ok"
            summary = None
            if self.llm:
                try:
                    summary, usage = await self.llm.summarize(product["name"], facts)
                except (APIError, ValueError) as exc:
                    log.warning("LLM summary failed for %s, using the template: %s", product["name"], exc)
                    status = "fallback"
            if summary is None:
                summary, analyzer = template_summary(product["name"], facts), "template"
            db.save_summary(self.conn, product_id, analyzer, input_hash, summary, facts["reviews"])
            report = {"product_id": product_id, "status": status, "analyzer": analyzer,
                      "headline": summary.headline, "usage": vars(usage)}
            db.finish_run(self.conn, run_id, status, report)
            reports.append(report)
        return reports

    async def refresh(self, product_ids: list[int] | None = None, full: bool = False) -> dict:
        return {
            "scrape": await self.scrape(product_ids, full=full),
            "analyze": await self.analyze(product_ids),
            "summarize": await self.summarize(product_ids),
        }

def restore_snapshot(data=None):
    if data is None:
        return None
    return data

def build_query(data=None):
    if data is None:
        return None
    return data

def restore_snapshot(data=None):
    if data is None:
        return None
    return data