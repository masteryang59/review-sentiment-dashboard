"""SQLite storage. One connection per process; every write commits immediately."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    selectors        TEXT,               -- JSON, for sites without structured review data
    created_at       TEXT NOT NULL,
    last_scraped_at  TEXT,
    last_scrape_status TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL,         -- the site's review id, or a hash when it has none
    author        TEXT,
    rating        REAL,                  -- normalized to 0-5
    title         TEXT,
    body          TEXT NOT NULL,
    published_on  TEXT,                  -- YYYY-MM-DD
    content_hash  TEXT NOT NULL,         -- changes when the review is edited, which triggers re-analysis
    scraped_at    TEXT NOT NULL,
    UNIQUE (product_id, source_id)
);
CREATE INDEX IF NOT EXISTS reviews_product_date ON reviews (product_id, published_on);

CREATE TABLE IF NOT EXISTS analyses (
    review_id      INTEGER PRIMARY KEY REFERENCES reviews(id) ON DELETE CASCADE,
    analyzer       TEXT NOT NULL,        -- "lexicon" or "openai:<model>"
    prompt_version TEXT NOT NULL,
    content_hash   TEXT NOT NULL,        -- the review content this analysis was made from
    sentiment      TEXT NOT NULL CHECK (sentiment IN ('positive', 'neutral', 'mixed', 'negative')),
    score          REAL NOT NULL,        -- -1 (very negative) to 1 (very positive)
    summary        TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_themes (
    review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    theme      TEXT NOT NULL,
    sentiment  TEXT NOT NULL CHECK (sentiment IN ('positive', 'negative')),
    evidence   TEXT,                     -- a quote from the review, verified to appear in it
    PRIMARY KEY (review_id, theme, sentiment)
);

CREATE TABLE IF NOT EXISTS product_summaries (
    product_id    INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    analyzer      TEXT NOT NULL,
    input_hash    TEXT NOT NULL,         -- skip regenerating when the inputs haven't changed
    headline      TEXT NOT NULL,
    strengths     TEXT NOT NULL,         -- JSON list
    issues        TEXT NOT NULL,         -- JSON list
    review_count  INTEGER NOT NULL,
    generated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,           -- scrape / analyze / summarize
    product_id  INTEGER,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    detail      TEXT                     -- JSON
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def content_hash(title: str | None, body: str, rating: float | None) -> str:
    raw = json.dumps([title or "", body, rating], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def connect(path: Path | str) -> sqlite3.Connection:
    in_memory = str(path) == ":memory:"
    if not in_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # The web app reads from a threadpool; writes happen from one place at a time.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not in_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


# --- products ---------------------------------------------------------------

def add_product(conn, name: str, url: str, selectors: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO products (name, url, selectors, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (url) DO UPDATE SET name = excluded.name, selectors = excluded.selectors "
        "RETURNING id",
        (name, url, json.dumps(selectors) if selectors else None, now_iso()),
    )
    product_id = cur.fetchone()[0]
    conn.commit()
    return product_id


def get_product(conn, product_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def list_products(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.*,
               COUNT(r.id) AS review_count,
               COUNT(a.review_id) AS analyzed_count,
               SUM(CASE WHEN a.analyzer = 'lexicon' THEN 1 ELSE 0 END) AS lexicon_count,
               AVG(r.rating) AS avg_rating
        FROM products p
        LEFT JOIN reviews r ON r.product_id = p.id
        LEFT JOIN analyses a ON a.review_id = r.id
        GROUP BY p.id ORDER BY p.id
        """
    ).fetchall()
    return [dict(r) | {"selectors": json.loads(r["selectors"]) if r["selectors"] else None} for r in rows]


def remove_product(conn, product_id: int) -> bool:
    deleted = conn.execute("DELETE FROM products WHERE id = ?", (product_id,)).rowcount
    conn.commit()
    return bool(deleted)


def mark_scraped(conn, product_id: int, status: str, name: str | None = None) -> None:
    conn.execute(
        "UPDATE products SET last_scraped_at = ?, last_scrape_status = ?, name = COALESCE(?, name) WHERE id = ?",
        (now_iso(), status, name, product_id),
    )
    conn.commit()


# --- reviews ----------------------------------------------------------------

def known_review_ids(conn, product_id: int) -> set[str]:
    rows = conn.execute("SELECT source_id FROM reviews WHERE product_id = ?", (product_id,))
    return {r[0] for r in rows}


def upsert_reviews(conn, product_id: int, reviews) -> tuple[int, int]:
    """Insert new reviews and update edited ones. Returns (inserted, updated)."""
    inserted = updated = 0
    scraped_at = now_iso()
    for r in reviews:
        digest = content_hash(r.title, r.body, r.rating)
        existing = conn.execute(
            "SELECT id, content_hash FROM reviews WHERE product_id = ? AND source_id = ?",
            (product_id, r.source_id),
        ).fetchone()
        published = r.published_on.isoformat() if r.published_on else None
        if existing is None:
            conn.execute(
                "INSERT INTO reviews (product_id, source_id, author, rating, title, body, published_on, "
                "content_hash, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product_id, r.source_id, r.author, r.rating, r.title, r.body, published, digest, scraped_at),
            )
            inserted += 1
        elif existing["content_hash"] != digest:
            conn.execute(
                "UPDATE reviews SET author = ?, rating = ?, title = ?, body = ?, published_on = ?, "
                "content_hash = ?, scraped_at = ? WHERE id = ?",
                (r.author, r.rating, r.title, r.body, published, digest, scraped_at, existing["id"]),
            )
            updated += 1
    conn.commit()
    return inserted, updated


def reviews_needing_analysis(conn, product_ids: list[int] | None, use_llm: bool,
                             reanalyze: bool = False, limit: int | None = None) -> list[sqlite3.Row]:
    """Reviews with no analysis, an analysis of older content, or (when an LLM is available)
    a lexicon fallback analysis that the LLM should redo."""
    where = ["1"] if reanalyze else [
        "(a.review_id IS NULL OR a.content_hash != r.content_hash"
        + (" OR a.analyzer = 'lexicon')" if use_llm else ")")
    ]
    params: list = []
    if product_ids:
        where.append(f"r.product_id IN ({','.join('?' * len(product_ids))})")
        params += product_ids
    sql = (
        "SELECT r.* FROM reviews r LEFT JOIN analyses a ON a.review_id = r.id "
        f"WHERE {' AND '.join(where)} ORDER BY r.published_on DESC, r.id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def save_analysis(conn, review: sqlite3.Row, analysis, analyzer: str, prompt_version: str) -> None:
    conn.execute("DELETE FROM review_themes WHERE review_id = ?", (review["id"],))
    conn.execute(
        "INSERT OR REPLACE INTO analyses (review_id, analyzer, prompt_version, content_hash, sentiment, score, "
        "summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (review["id"], analyzer, prompt_version, review["content_hash"], analysis.sentiment,
         analysis.score, analysis.summary, now_iso()),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO review_themes (review_id, theme, sentiment, evidence) VALUES (?, ?, ?, ?)",
        [(review["id"], t.theme, t.sentiment, t.evidence or None) for t in analysis.themes],
    )


# --- summaries & runs -------------------------------------------------------

def get_summary(conn, product_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM product_summaries WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        return None
    return dict(row) | {"strengths": json.loads(row["strengths"]), "issues": json.loads(row["issues"])}


def save_summary(conn, product_id: int, analyzer: str, input_hash: str, summary, review_count: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO product_summaries (product_id, analyzer, input_hash, headline, strengths, issues, "
        "review_count, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, analyzer, input_hash, summary.headline, json.dumps(summary.strengths),
         json.dumps(summary.issues), review_count, now_iso()),
    )
    conn.commit()


def start_run(conn, kind: str, product_id: int | None = None) -> int:
    cur = conn.execute("INSERT INTO runs (kind, product_id, started_at) VALUES (?, ?, ?)",
                       (kind, product_id, now_iso()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str, detail: dict) -> None:
    conn.execute("UPDATE runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
                 (now_iso(), status, json.dumps(detail), run_id))
    conn.commit()


def recent_runs(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) | {"detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

def revoke_access(data=None):
    if data is None:
        return None
    return data

def handle_error(data=None):
    if data is None:
        return None
    return data

def revoke_access(data=None):
    if data is None:
        return None
    return data