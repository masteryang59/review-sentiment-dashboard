import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from . import db
from .config import Settings
from .llm import LLMConfigError
from .service import Service

app = typer.Typer(help="Scrape product reviews, score sentiment and themes with GPT, and chart them.",
                  no_args_is_help=True, add_completion=False)
console = Console()

IdsArg = Annotated[Optional[list[int]], typer.Argument(help="Product ids (default: all)")]


@app.callback()
def main(verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Log every request and LLM batch")] = False):
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


def _service() -> Service:
    settings = Settings()
    return Service(db.connect(settings.db_path), settings)


@app.command()
def add(url: str,
        name: Annotated[Optional[str], typer.Option(help="Display name (default: taken from the page)")] = None,
        selectors: Annotated[Optional[str], typer.Option(
            help='CSS selectors as JSON or a path to a JSON file, for pages without structured review data')] = None):
    """Track a product's review page."""
    parsed = None
    if selectors:
        path = Path(selectors)
        parsed = json.loads(path.read_text(encoding="utf-8") if path.exists() else selectors)
    try:
        product_id = _service().add_product(url, name, parsed)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Added product [bold]{product_id}[/]: {name or url}")


@app.command()
def remove(product_id: int):
    """Stop tracking a product and delete its reviews."""
    service = _service()
    if not db.remove_product(service.conn, product_id):
        console.print(f"[red]No product {product_id}[/]")
        raise typer.Exit(1)
    console.print(f"Removed product {product_id}")


@app.command("list")
def list_products():
    """Products with review counts and analysis coverage."""
    service = _service()
    table = Table("ID", "Name", "Reviews", "Analyzed", "Avg ★", "Last scrape", "Status")
    for p in db.list_products(service.conn):
        analyzed = f"{p['analyzed_count']}" + (f" ({p['lexicon_count']} lexicon)" if p["lexicon_count"] and service.llm else "")
        table.add_row(str(p["id"]), p["name"], str(p["review_count"]), analyzed,
                      f"{p['avg_rating']:.2f}" if p["avg_rating"] is not None else "-",
                      (p["last_scraped_at"] or "never").replace("T", " ")[:16], p["last_scrape_status"] or "-")
    console.print(table)
    console.print(f"Analyzer: [bold]{service.analyzer_name}[/]")


def _print_scrape(reports: list[dict]) -> None:
    table = Table("ID", "Product", "Status", "Pages", "Found", "New", "Updated", "Stopped", "Via")
    for r in reports:
        if r["status"] == "missing":
            table.add_row(str(r["product_id"]), "-", "[red]no such product[/]", "", "", "", "", "", "")
            continue
        colour = {"ok": "green", "partial": "yellow"}.get(r["status"], "red")
        table.add_row(str(r["product_id"]), r["name"], f"[{colour}]{r['status']}[/]", str(r["pages"]), str(r["found"]),
                      str(r["new"]), str(r["updated"]), r["stopped"], ", ".join(r["methods"]) or "-")
    console.print(table)
    for r in reports:
        if r.get("error"):
            console.print(f"[yellow]{r['name']}: {r['error']}[/]")


def _print_analyze(report: dict) -> None:
    usage = report["usage"]
    line = f"Analyzed [bold]{report['analyzed']}[/] of {report['pending']} with {report['analyzer']}"
    if report["fallback"]:
        line += f", [yellow]{report['fallback']} fell back to the lexicon[/] (retried next run)"
    console.print(line)
    if usage["calls"]:
        console.print(f"LLM: {usage['calls']} calls, {usage['prompt_tokens']:,} prompt + "
                      f"{usage['completion_tokens']:,} completion tokens")
    if report["evidence_dropped"] or report["themes_dropped"]:
        console.print(f"Dropped {report['evidence_dropped']} quotes not found in the review and "
                      f"{report['themes_dropped']} themes outside the taxonomy")


def _print_summaries(reports: list[dict]) -> None:
    for r in reports:
        if r["status"] in ("ok", "fallback"):
            console.print(f"[bold]#{r['product_id']}[/] ({r['analyzer']}): {r['headline']}")


async def _analyze(service: Service, ids, reanalyze: bool, limit) -> dict:
    with Progress(console=console, transient=True) as progress:
        task = progress.add_task(f"Analyzing with {service.analyzer_name}", total=None)
        return await service.analyze(ids, reanalyze=reanalyze, limit=limit,
                                     on_progress=lambda done, total: progress.update(task, completed=done, total=total))


def _run(coro):
    try:
        return asyncio.run(coro)
    except LLMConfigError as exc:
        console.print(f"[red]LLM request rejected: {exc}[/]")
        raise typer.Exit(2) from exc


@app.command()
def scrape(ids: IdsArg = None,
           full: Annotated[bool, typer.Option(help="Re-read every page instead of stopping at known reviews")] = False):
    """Fetch new and edited reviews."""
    _print_scrape(_run(_service().scrape(ids, full=full)))


@app.command()
def analyze(ids: IdsArg = None,
            reanalyze: Annotated[bool, typer.Option(help="Redo reviews that already have an analysis")] = False,
            limit: Annotated[Optional[int], typer.Option(help="Analyze at most this many reviews")] = None):
    """Score sentiment and themes for reviews that need it."""
    service = _service()
    _print_analyze(_run(_analyze(service, ids, reanalyze, limit)))


@app.command()
def summarize(ids: IdsArg = None, force: Annotated[bool, typer.Option(help="Regenerate unchanged summaries")] = False):
    """Write each product's summary panel (only when its numbers changed)."""
    _print_summaries(_run(_service().summarize(ids, force=force)))


@app.command()
def refresh(ids: IdsArg = None, full: bool = False):
    """scrape + analyze + summarize."""
    service = _service()

    async def go():
        scraped = await service.scrape(ids, full=full)
        _print_scrape(scraped)
        _print_analyze(await _analyze(service, ids, False, None))
        _print_summaries(await service.summarize(ids))

    _run(go())


@app.command("seed-demo")
def seed_demo(base_url: Annotated[Optional[str], typer.Option(help="Demo store URL (default: DEMO_STORE_URL)")] = None):
    """Track the four demo store products."""
    from demo_store.server import PLAIN_SELECTORS

    service = _service()
    base = (base_url or service.settings.demo_store_url).rstrip("/")
    products = [("Aurora ANC Headphones", "aurora-anc-headphones", None),
                ("Ember Gooseneck Kettle", "ember-gooseneck-kettle", None),
                ("Ridgeline Trail Boots", "ridgeline-trail-boots", PLAIN_SELECTORS),
                ("Nimbus Robot Vacuum", "nimbus-robot-vacuum", None)]
    for name, slug, selectors in products:
        product_id = service.add_product(f"{base}/products/{slug}", name, selectors)
        console.print(f"[bold]{product_id}[/] {name}" + (" (with CSS selectors)" if selectors else ""))


@app.command()
def runs(limit: int = 15):
    """Recent scrape / analyze / summarize runs."""
    table = Table("ID", "Kind", "Product", "Started", "Status", "Detail")
    for r in db.recent_runs(_service().conn, limit):
        detail = r["detail"] or {}
        brief = {k: detail[k] for k in ("new", "pages", "analyzed", "fallback", "analyzer", "error") if detail.get(k)}
        table.add_row(str(r["id"]), r["kind"], str(r["product_id"] or ""), r["started_at"].replace("T", " ")[:19],
                      r["status"] or "running", json.dumps(brief))
    console.print(table)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Run the dashboard."""
    import uvicorn

    from .web import create_app

    console.print(f"Dashboard on http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")

def archive_record(data=None):
    if data is None:
        return None
    return data

def validate_request(data=None):
    if data is None:
        return None
    return data

def archive_record(data=None):
    if data is None:
        return None
    return data

def validate_request(data=None):
    if data is None:
        return None
    return data