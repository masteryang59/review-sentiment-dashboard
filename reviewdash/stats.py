"""Aggregations behind the dashboard and the product summary."""

import hashlib
import json
from datetime import date, timedelta

from .config import Theme

SENTIMENTS = ("positive", "neutral", "mixed", "negative")
MISMATCH_SQL = "((r.rating >= 4 AND a.sentiment = 'negative') OR (r.rating <= 2 AND a.sentiment = 'positive'))"


def _range_sql(since: str | None, until: str | None) -> tuple[str, list]:
    clauses, params = [], []
    if since:
        clauses.append("r.published_on >= ?")
        params.append(since)
    if until:
        clauses.append("r.published_on <= ?")
        params.append(until)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _months(first: str, last: str) -> list[str]:
    year, month = int(first[:4]), int(first[5:7])
    out = []
    while f"{year:04d}-{month:02d}" <= last[:7]:
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def _totals(conn, product_id: int, since: str | None, until: str | None) -> dict:
    rng, params = _range_sql(since, until)
    row = conn.execute(
        f"""
        SELECT COUNT(r.id) AS reviews, AVG(r.rating) AS avg_rating, COUNT(a.review_id) AS analyzed,
               SUM(a.sentiment = 'positive') AS positive, SUM(a.sentiment = 'neutral') AS neutral,
               SUM(a.sentiment = 'mixed') AS mixed, SUM(a.sentiment = 'negative') AS negative,
               AVG(a.score) AS avg_score,
               SUM(CASE WHEN {MISMATCH_SQL} THEN 1 ELSE 0 END) AS mismatches
        FROM reviews r LEFT JOIN analyses a ON a.review_id = r.id
        WHERE r.product_id = ? {rng}
        """,
        [product_id, *params],
    ).fetchone()
    totals = {k: (row[k] or 0) for k in row.keys()}
    totals["avg_rating"] = round(row["avg_rating"], 2) if row["avg_rating"] is not None else None
    totals["avg_score"] = round(row["avg_score"], 3) if row["avg_score"] is not None else None
    analyzed = totals["analyzed"]
    totals["net_sentiment"] = round((totals["positive"] - totals["negative"]) / analyzed * 100, 1) if analyzed else None
    return totals


def product_stats(conn, product_id: int, themes: tuple[Theme, ...], since: str | None = None,
                  until: str | None = None) -> dict:
    totals = _totals(conn, product_id, since, until)

    previous = None
    if since:
        start = date.fromisoformat(since)
        end = date.fromisoformat(until) if until else date.today()
        length = (end - start).days + 1
        prev_until = start - timedelta(days=1)
        prev_since = prev_until - timedelta(days=length - 1)
        previous = _totals(conn, product_id, prev_since.isoformat(), prev_until.isoformat())
        previous |= {"since": prev_since.isoformat(), "until": prev_until.isoformat()}

    rng, params = _range_sql(since, until)
    rows = conn.execute(
        f"""
        SELECT substr(r.published_on, 1, 7) AS month, COUNT(r.id) AS total, AVG(r.rating) AS avg_rating,
               SUM(a.sentiment = 'positive') AS positive, SUM(a.sentiment = 'neutral') AS neutral,
               SUM(a.sentiment = 'mixed') AS mixed, SUM(a.sentiment = 'negative') AS negative
        FROM reviews r LEFT JOIN analyses a ON a.review_id = r.id
        WHERE r.product_id = ? AND r.published_on IS NOT NULL {rng}
        GROUP BY month ORDER BY month
        """,
        [product_id, *params],
    ).fetchall()
    by_month = {r["month"]: r for r in rows}
    monthly = []
    if rows:
        first = since[:7] if since else rows[0]["month"]
        last = (until or rows[-1]["month"])[:7]
        for month in _months(min(first, rows[0]["month"]), max(last, rows[-1]["month"])):
            r = by_month.get(month)
            monthly.append({
                "month": month,
                "total": r["total"] if r else 0,
                "avg_rating": round(r["avg_rating"], 2) if r and r["avg_rating"] is not None else None,
                **{s: (r[s] or 0) if r else 0 for s in SENTIMENTS},
            })

    theme_rows = conn.execute(
        f"""
        SELECT t.theme, SUM(t.sentiment = 'positive') AS positive, SUM(t.sentiment = 'negative') AS negative
        FROM review_themes t JOIN reviews r ON r.id = t.review_id
        WHERE r.product_id = ? {rng}
        GROUP BY t.theme
        """,
        [product_id, *params],
    ).fetchall()
    labels = {t.key: t.label for t in themes}
    theme_stats = sorted(
        ({"theme": r["theme"], "label": labels.get(r["theme"], r["theme"]), "positive": r["positive"],
          "negative": r["negative"], "total": r["positive"] + r["negative"]} for r in theme_rows),
        key=lambda t: (-t["total"], t["theme"]),
    )

    analyzers = {r[0]: r[1] for r in conn.execute(
        f"SELECT a.analyzer, COUNT(*) FROM analyses a JOIN reviews r ON r.id = a.review_id "
        f"WHERE r.product_id = ? {rng} GROUP BY a.analyzer",
        [product_id, *params],
    )}
    span = conn.execute(
        "SELECT MIN(published_on), MAX(published_on) FROM reviews WHERE product_id = ?", (product_id,),
    ).fetchone()
    return {
        "range": {"since": since, "until": until, "first_review": span[0], "last_review": span[1]},
        "totals": totals,
        "previous": previous,
        "monthly": monthly,
        "themes": theme_stats,
        "analyzers": analyzers,
    }


def list_reviews(conn, product_id: int, since: str | None = None, until: str | None = None,
                 sentiment: str | None = None, theme: str | None = None, theme_sentiment: str | None = None,
                 mismatch: bool = False, q: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    rng, params = _range_sql(since, until)
    where = f"r.product_id = ? {rng}"
    params = [product_id, *params]
    if sentiment:
        where += " AND a.sentiment = ?"
        params.append(sentiment)
    if theme:
        where += " AND EXISTS (SELECT 1 FROM review_themes t WHERE t.review_id = r.id AND t.theme = ?"
        params.append(theme)
        if theme_sentiment:
            where += " AND t.sentiment = ?"
            params.append(theme_sentiment)
        where += ")"
    if mismatch:
        where += f" AND {MISMATCH_SQL}"
    if q:
        where += " AND (r.body LIKE ? ESCAPE '\\' OR r.title LIKE ? ESCAPE '\\')"
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like, like]
    total = conn.execute(
        f"SELECT COUNT(*) FROM reviews r LEFT JOIN analyses a ON a.review_id = r.id WHERE {where}", params,
    ).fetchone()[0]
    page = max(1, page)
    rows = conn.execute(
        f"""
        SELECT r.id, r.source_id, r.author, r.rating, r.title, r.body, r.published_on,
               a.sentiment, a.score, a.summary, a.analyzer,
               CASE WHEN {MISMATCH_SQL} THEN 1 ELSE 0 END AS mismatch
        FROM reviews r LEFT JOIN analyses a ON a.review_id = r.id
        WHERE {where}
        ORDER BY r.published_on DESC, r.id DESC LIMIT ? OFFSET ?
        """,
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    ids = [r["id"] for r in rows]
    themes: dict[int, list] = {i: [] for i in ids}
    if ids:
        for t in conn.execute(
            f"SELECT review_id, theme, sentiment, evidence FROM review_themes "
            f"WHERE review_id IN ({','.join('?' * len(ids))}) ORDER BY theme",
            ids,
        ):
            themes[t["review_id"]].append({"theme": t["theme"], "sentiment": t["sentiment"], "evidence": t["evidence"]})
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "reviews": [dict(r) | {"mismatch": bool(r["mismatch"]), "themes": themes[r["id"]]} for r in rows],
    }


def summary_facts(conn, product_id: int, themes: tuple[Theme, ...], quotes_per_theme: int = 3) -> dict:
    """The compact, numbers-first input for the product summary (LLM or template)."""
    stats = product_stats(conn, product_id, themes)
    totals = stats["totals"]
    last = stats["range"]["last_review"]
    rising = []
    if last:
        end = date.fromisoformat(last)
        recent_since = (end - timedelta(days=89)).isoformat()
        before_since = (end - timedelta(days=179)).isoformat()
        before_until = (end - timedelta(days=90)).isoformat()
        counts = {}
        for label, lo, hi in (("recent", recent_since, last), ("before", before_since, before_until)):
            for row in conn.execute(
                "SELECT t.theme, t.sentiment, COUNT(*) FROM review_themes t JOIN reviews r ON r.id = t.review_id "
                "WHERE r.product_id = ? AND r.published_on BETWEEN ? AND ? GROUP BY t.theme, t.sentiment",
                (product_id, lo, hi),
            ):
                counts[(label, row[0], row[1])] = row[2]
        labels = {t.key: t.label for t in themes}
        for theme in {k[1] for k in counts}:
            for polarity in ("negative", "positive"):
                recent, before = counts.get(("recent", theme, polarity), 0), counts.get(("before", theme, polarity), 0)
                if recent >= 5 and recent >= 2 * max(before, 1):
                    rising.append({"theme": labels.get(theme, theme), "sentiment": polarity,
                                   "last_90_days": recent, "previous_90_days": before})
        rising.sort(key=lambda r: (r["sentiment"] != "negative", -r["last_90_days"]))

    quotes = {}
    for t in stats["themes"][:6]:
        for polarity in ("positive", "negative"):
            rows = conn.execute(
                "SELECT t.evidence FROM review_themes t JOIN reviews r ON r.id = t.review_id "
                "WHERE r.product_id = ? AND t.theme = ? AND t.sentiment = ? AND t.evidence IS NOT NULL "
                "ORDER BY r.published_on DESC LIMIT ?",
                (product_id, t["theme"], polarity, quotes_per_theme),
            ).fetchall()
            if rows:
                quotes[f"{t['label']} ({polarity})"] = [r[0] for r in rows]
    return {
        "reviews": totals["reviews"],
        "date_range": [stats["range"]["first_review"], last],
        "average_rating": totals["avg_rating"],
        "sentiment_counts": {s: totals[s] for s in SENTIMENTS},
        "net_sentiment": totals["net_sentiment"],
        "rating_text_mismatches": totals["mismatches"],
        "themes": [{k: t[k] for k in ("theme", "label", "positive", "negative")} for t in stats["themes"]],
        "trends_last_90_days": rising,
        "quotes": quotes,
    }


def facts_hash(facts: dict) -> str:
    return hashlib.sha1(json.dumps(facts, sort_keys=True).encode("utf-8")).hexdigest()[:16]

def emit_event(data=None):
    if data is None:
        return None
    return data

def log_event(data=None):
    if data is None:
        return None
    return data

def emit_event(data=None):
    if data is None:
        return None
    return data