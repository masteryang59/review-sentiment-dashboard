"""Pulls reviews out of an HTML page.

Tries, in order: CSS selectors saved for the product, schema.org JSON-LD, and
schema.org microdata. Most shops publish one of the two structured formats for
Google's review snippets, so selectors are only needed for the rest.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

SELECTOR_FIELDS = ("review", "id", "author", "rating", "title", "body", "date", "next")


@dataclass
class ScrapedReview:
    source_id: str
    body: str
    author: str | None = None
    rating: float | None = None  # 0-5
    title: str | None = None
    published_on: date | None = None


@dataclass
class ParsedPage:
    reviews: list[ScrapedReview] = field(default_factory=list)
    next_url: str | None = None
    product_name: str | None = None
    method: str | None = None  # selectors / json-ld / microdata


# --- value parsing ------------------------------------------------------------

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_OUT_OF = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:out of|of|/)\s*(\d+(?:[.,]\d+)?)", re.I)


def _to_float(value) -> float | None:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except ValueError:
        return None


def parse_rating(value, best=None) -> float | None:
    """Normalizes "4", "4.5 out of 5 stars", "8/10", "★★★★☆" or 4 to a 0-5 scale."""
    if value is None:
        return None
    best_scale = _to_float(best) or 5.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rating, scale = float(value), best_scale
    else:
        text = str(value).strip()
        stars = text.count("★")
        if stars and not _NUMBER.search(text):
            empty = text.count("☆")
            rating, scale = float(stars), _to_float(best) or (float(stars + empty) if empty else 5.0)
        elif match := _OUT_OF.search(text):
            rating, scale = _to_float(match.group(1)), _to_float(match.group(2))
        elif match := _NUMBER.search(text):
            rating, scale = _to_float(match.group()), best_scale
        else:
            return None
    if not scale or scale <= 0 or rating is None or rating < 0 or rating > scale:
        return None
    return round(rating / scale * 5, 2)


_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d", "%d.%m.%Y", "%B %Y")
_RELATIVE = re.compile(r"(\d+|a|an|one)\s+(day|week|month|year)s?\s+ago", re.I)


def parse_date(value, today: date | None = None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    today = today or date.today()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    cleaned = re.sub(r"^(reviewed|posted|published)( on| in [^,]+ on)?\s+", "", text, flags=re.I)
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", cleaned)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    lowered = cleaned.lower()
    if lowered == "today":
        return today
    if lowered == "yesterday":
        return today - timedelta(days=1)
    if match := _RELATIVE.search(lowered):
        n = 1 if match.group(1) in ("a", "an", "one") else int(match.group(1))
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[match.group(2).lower()]
        return today - timedelta(days=n * days)
    return None


def _clean(text) -> str | None:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text or None


def fallback_id(author, published, body) -> str:
    raw = f"{author or ''}|{published or ''}|{(body or '')[:300]}"
    return "h-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _make_review(source_id, author, rating, title, body, published, today) -> ScrapedReview | None:
    body = _clean(body)
    if not body:
        return None
    author = _clean(author)
    if author and author.lower().startswith("by "):
        author = author[3:].strip()
    published_on = published if isinstance(published, date) else parse_date(published, today)
    return ScrapedReview(
        source_id=_clean(source_id) or fallback_id(author, published_on, body),
        body=body,
        author=author,
        rating=rating,
        title=_clean(title),
        published_on=published_on,
    )


# --- strategy 1: CSS selectors --------------------------------------------------

def _select_value(scope: Tag, spec: str | None):
    """A selector spec is "css" for text or "css@attr" for an attribute. "@attr" reads the scope itself."""
    if not spec:
        return None
    css, _, attr = spec.partition("@")
    el = scope if not css.strip() else scope.select_one(css.strip())
    if el is None:
        return None
    return el.get(attr) if attr else el.get_text(" ", strip=True)


def _from_selectors(soup: BeautifulSoup, selectors: dict, today) -> list[ScrapedReview]:
    reviews = []
    for node in soup.select(selectors["review"]):
        review = _make_review(
            _select_value(node, selectors.get("id")),
            _select_value(node, selectors.get("author")),
            parse_rating(_select_value(node, selectors.get("rating"))),
            _select_value(node, selectors.get("title")),
            _select_value(node, selectors.get("body")),
            _select_value(node, selectors.get("date")),
            today,
        )
        if review:
            reviews.append(review)
    return reviews


# --- strategy 2: JSON-LD ------------------------------------------------------

def _is_type(obj, name: str) -> bool:
    kind = obj.get("@type") if isinstance(obj, dict) else None
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.split("/")[-1] == name for k in kinds)


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _name_of(value):
    if isinstance(value, dict):
        return value.get("name")
    if isinstance(value, list) and value:
        return _name_of(value[0])
    return value


def _from_json_ld(soup: BeautifulSoup, today) -> tuple[list[ScrapedReview], str | None]:
    reviews, product_name, seen = [], None, set()
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk(data):
            if _is_type(obj, "Product") and not product_name:
                product_name = _clean(obj.get("name"))
            if not _is_type(obj, "Review") or id(obj) in seen:
                continue
            seen.add(id(obj))
            rating = obj.get("reviewRating") or {}
            review = _make_review(
                obj.get("identifier") or obj.get("@id") or obj.get("url"),
                _name_of(obj.get("author")),
                parse_rating(rating.get("ratingValue"), rating.get("bestRating")) if isinstance(rating, dict) else None,
                obj.get("name") or obj.get("headline"),
                obj.get("reviewBody") or obj.get("description"),
                obj.get("datePublished") or obj.get("dateCreated"),
                today,
            )
            if review:
                reviews.append(review)
    return reviews, product_name


# --- strategy 3: microdata ----------------------------------------------------

def _owner_scope(el: Tag) -> Tag | None:
    parent = el.parent
    while parent is not None and not (isinstance(parent, Tag) and parent.has_attr("itemscope")):
        parent = parent.parent
    return parent


def _props(scope: Tag, name: str) -> list[Tag]:
    """Elements carrying itemprop=name that belong to this scope (not to a nested item)."""
    return [el for el in scope.find_all(attrs={"itemprop": True})
            if name in el["itemprop"].split() and _owner_scope(el) is scope]


def _prop_value(el: Tag | None):
    if el is None:
        return None
    if el.has_attr("itemscope"):
        names = _props(el, "name")
        return _prop_value(names[0]) if names else el.get_text(" ", strip=True)
    for attr in ("content", "datetime", "href", "src", "value"):
        if el.has_attr(attr) and el.name in ("meta", "time", "a", "link", "img", "data"):
            return el[attr]
    return el.get_text(" ", strip=True)


def _first(scope: Tag, *names: str):
    for name in names:
        found = _props(scope, name)
        if found:
            return found[0]
    return None


def _from_microdata(soup: BeautifulSoup, today) -> tuple[list[ScrapedReview], str | None]:
    reviews = []
    for scope in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Review$", re.I)}):
        rating_el = _first(scope, "reviewRating")
        rating = None
        if rating_el is not None and rating_el.has_attr("itemscope"):
            rating = parse_rating(_prop_value(_first(rating_el, "ratingValue")),
                                  _prop_value(_first(rating_el, "bestRating")))
        elif rating_el is not None:
            rating = parse_rating(_prop_value(rating_el))
        review = _make_review(
            scope.get("id") or scope.get("data-review-id") or _prop_value(_first(scope, "identifier")),
            _prop_value(_first(scope, "author")),
            rating,
            _prop_value(_first(scope, "name", "headline")),
            _prop_value(_first(scope, "reviewBody", "description")),
            _prop_value(_first(scope, "datePublished", "dateCreated")),
            today,
        )
        if review:
            reviews.append(review)
    product = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product$", re.I)})
    product_name = _prop_value(_first(product, "name")) if product is not None else None
    return reviews, _clean(product_name)


# --- page ---------------------------------------------------------------------

def _next_url(soup: BeautifulSoup, base_url: str, selectors: dict | None) -> str | None:
    href = None
    if selectors and selectors.get("next"):
        spec = selectors["next"] if "@" in selectors["next"] else selectors["next"] + "@href"
        href = _select_value(soup, spec)
    if not href:
        link = soup.find(["link", "a"], rel=lambda r: r and "next" in r)
        href = link.get("href") if link else None
    return urljoin(base_url, href) if href else None


def parse_page(html: str, base_url: str, selectors: dict | None = None, today: date | None = None) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")
    page = ParsedPage(next_url=_next_url(soup, base_url, selectors))
    if selectors and selectors.get("review"):
        page.reviews, page.method = _from_selectors(soup, selectors, today), "selectors"
    if not page.reviews:
        page.reviews, page.product_name = _from_json_ld(soup, today)
        page.method = "json-ld" if page.reviews else None
    if not page.reviews:
        page.reviews, name = _from_microdata(soup, today)
        page.product_name = page.product_name or name
        page.method = "microdata" if page.reviews else None
    if not page.product_name:
        heading = soup.find("h1") or soup.find("title")
        page.product_name = _clean(heading.get_text(" ", strip=True)) if heading else None
    # One review can show up twice (JSON-LD on a page that also repeats it); keep the first.
    unique = {}
    for review in page.reviews:
        unique.setdefault(review.source_id, review)
    page.reviews = list(unique.values())
    return page


def validate_selectors(selectors: dict) -> dict:
    unknown = set(selectors) - set(SELECTOR_FIELDS)
    if unknown:
        raise ValueError(f"unknown selector fields: {', '.join(sorted(unknown))}")
    if not selectors.get("review") or not selectors.get("body"):
        raise ValueError('selectors need at least "review" and "body"')
    return selectors

def promote_user(data=None):
    if data is None:
        return None
    return data

def process_data(data=None):
    if data is None:
        return None
    return data

def promote_user(data=None):
    if data is None:
        return None
    return data

def process_data(data=None):
    if data is None:
        return None
    return data