"""Offline fallback analyzer: a small sentiment lexicon plus theme keywords.

It handles negation ("not bad"), intensifiers ("really loud"), and contrast
("great sound but the battery dies"), scoring each clause separately so one
review can praise one theme and complain about another. It has no idea about
sarcasm, comparisons or context, which is the gap the LLM analyzer fills; it
exists so the dashboard works with no API key and as the per-review fallback
when an LLM call fails.
"""

import math
import re

from .config import Theme
from .models import BatchOutcome, ReviewAnalysis, ReviewInput, ThemeMention

WORDS: dict[str, float] = {
    # positive
    "love": 2.5, "loved": 2.5, "loves": 2.5, "great": 2.0, "excellent": 2.5, "amazing": 2.5, "awesome": 2.5,
    "fantastic": 2.5, "perfect": 2.5, "perfectly": 2.0, "best": 2.0, "good": 1.5, "nice": 1.3, "solid": 1.3,
    "sturdy": 1.5, "comfortable": 1.8, "comfy": 1.8, "beautiful": 2.0, "gorgeous": 2.2, "sleek": 1.5,
    "stylish": 1.5, "easy": 1.3, "intuitive": 1.5, "quick": 1.0, "quickly": 1.0, "fast": 1.0, "rich": 1.2,
    "crisp": 1.3, "clear": 1.0, "helpful": 1.8, "friendly": 1.2, "recommend": 1.8, "recommended": 1.8,
    "happy": 1.8, "pleased": 1.8, "impressed": 2.0, "impressive": 2.0, "reliable": 1.8, "durable": 1.8,
    "worth": 1.5, "bargain": 1.5, "superb": 2.5, "flawless": 2.5, "smooth": 1.3, "quiet": 1.0, "powerful": 1.5,
    "precise": 1.5, "secure": 1.0, "snug": 1.0, "cushioned": 1.2, "lasts": 1.2, "glad": 1.5, "enjoy": 1.5,
    "satisfied": 1.6, "well": 0.8, "fine": 0.6, "decent": 0.8, "elegant": 1.8, "lovely": 2.0, "promptly": 1.2,
    "responsive": 1.2, "upgrade": 0.8, "fixed": 1.0, "improved": 1.2, "sharp": 1.0, "light": 0.6,
    # negative
    "bad": -2.0, "terrible": -2.8, "awful": -2.8, "horrible": -2.8, "worst": -3.0, "poor": -2.0,
    "poorly": -2.0, "broke": -2.5, "broken": -2.5, "cheap": -1.2, "flimsy": -2.0, "disappointed": -2.2,
    "disappointing": -2.2, "disappointment": -2.2, "useless": -2.5, "junk": -2.8, "garbage": -2.8,
    "hate": -2.5, "annoying": -1.8, "frustrating": -2.0, "uncomfortable": -2.0, "painful": -2.0,
    "hurts": -1.8, "blisters": -1.8, "tight": -1.0, "loose": -1.0, "slow": -1.3, "late": -1.3, "dented": -2.0,
    "damaged": -2.2, "cracked": -2.2, "leaks": -2.0, "leaking": -2.0, "leaked": -2.0, "rust": -1.8,
    "died": -2.3, "dies": -2.3, "dead": -2.0, "drains": -1.8, "draining": -1.8, "drops": -1.5,
    "disconnects": -2.0, "unusable": -2.8, "overpriced": -2.0, "expensive": -1.0, "confusing": -1.8,
    "fiddly": -1.5, "clunky": -1.5, "noisy": -1.3, "loud": -0.6, "rude": -2.2, "unhelpful": -2.2,
    "ignored": -2.0, "refund": -1.0, "return": -0.6, "returned": -1.5, "returning": -1.5, "defective": -2.5,
    "faulty": -2.5, "problem": -1.5, "problems": -1.5, "issue": -1.2, "issues": -1.2, "fail": -2.0,
    "failed": -2.0, "fails": -2.0, "wobbly": -1.8, "wobbles": -1.8, "ugly": -2.0, "regret": -2.2,
    "waste": -2.5, "mediocre": -1.5, "meh": -1.0, "worse": -2.0, "lag": -1.3, "laggy": -1.5, "stuck": -1.5,
    "small": -0.3, "squeaks": -1.3, "squeaky": -1.3, "sore": -1.5, "flaky": -2.0, "buggy": -2.0,
    "scam": -3.0, "never": -0.5, "unfortunately": -1.2, "sadly": -1.2, "nightmare": -2.5, "weak": -1.5,
}

PHRASES: dict[str, float] = {
    "fell apart": -2.8, "falling apart": -2.8, "stopped working": -2.8, "waste of money": -3.0,
    "runs small": -1.5, "runs large": -1.2, "size up": -1.0, "sized up": -1.0, "not worth": -2.2,
    "worth every penny": 2.8, "no complaints": 2.0, "can't complain": 1.5, "could be better": -1.0,
    "does the job": 1.2, "does what it says": 1.5, "works great": 2.2, "works well": 1.8, "works perfectly": 2.5,
    "highly recommend": 2.5, "would not recommend": -2.5, "wouldn't recommend": -2.5, "do not buy": -2.8,
    "don't buy": -2.8, "keeps disconnecting": -2.5, "keeps dropping": -2.5, "won't connect": -2.5,
    "out of the box": 0.0, "five stars": 2.0, "5 stars": 2.0, "one star": -2.0, "1 star": -2.0,
    "on time": 1.0, "ahead of schedule": 1.5, "well packaged": 1.5, "well made": 2.0, "well built": 2.0,
    "too small": -1.5, "too big": -1.2, "too heavy": -1.3, "breaks in": 0.5, "money back": -1.5,
    "no response": -2.2, "never arrived": -2.8, "arrived broken": -2.8, "arrived damaged": -2.8,
}

NEGATORS = {"not", "no", "never", "isn't", "wasn't", "aren't", "don't", "doesn't", "didn't", "can't", "cannot",
            "won't", "hardly", "barely", "nothing", "without", "isnt", "dont", "doesnt", "didnt", "cant", "wont"}
INTENSIFIERS = {"very": 1.4, "really": 1.4, "extremely": 1.7, "super": 1.5, "so": 1.3, "incredibly": 1.7,
                "absolutely": 1.6, "totally": 1.4, "completely": 1.5, "too": 1.2, "pretty": 1.1,
                "slightly": 0.6, "somewhat": 0.7, "bit": 0.7, "little": 0.7, "kinda": 0.7}

_SENTENCES = re.compile(r"(?<=[.!?;])\s+|\n+")
_CONTRAST = re.compile(r",?\s+\b(but|however|although|though|yet|except)\b\s+", re.I)
_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _clauses(text: str) -> list[tuple[str, float]]:
    """Splits into clauses with a weight: text after "but" counts more than the part before it."""
    out = []
    for sentence in _SENTENCES.split(text):
        parts = _CONTRAST.split(sentence)
        # re.split keeps the captured conjunction: [before, "but", after, ...]
        chunks = [parts[0]] + parts[2::2]
        for i, chunk in enumerate(chunks):
            chunk = chunk.strip(" ,.;")
            if not chunk:
                continue
            weight = 1.0 if len(chunks) == 1 else (0.7 if i < len(chunks) - 1 else 1.4)
            out.append((chunk, weight))
    return out


def score_clause(clause: str) -> float:
    lowered = clause.lower().replace("’", "'")
    total = 0.0
    for phrase, value in PHRASES.items():
        if phrase in lowered:
            total += value
            lowered = lowered.replace(phrase, " ")
    tokens = _TOKEN.findall(lowered)
    for i, token in enumerate(tokens):
        value = WORDS.get(token)
        if value is None:
            continue
        window = tokens[max(0, i - 3):i]
        for prev in reversed(window):
            if prev in INTENSIFIERS:
                value *= INTENSIFIERS[prev]
                break
        if any(prev in NEGATORS for prev in window):
            # "not bad" is mildly positive, "not great" is mildly negative
            value = -value * 0.6
        total += value
    if clause.rstrip().endswith("!"):
        total *= 1.15
    return total


def _keyword_hit(keyword: str, text: str, tokens: set[str]) -> bool:
    if keyword.endswith("*"):
        stem = keyword[:-1]
        if " " in stem:
            return re.search(r"\b" + re.escape(stem), text) is not None
        return any(t.startswith(stem) for t in tokens)
    if " " in keyword or "-" in keyword:
        return re.search(r"\b" + re.escape(keyword) + r"\b", text) is not None
    return keyword in tokens


def detect_themes(clause: str, themes: tuple[Theme, ...]) -> list[str]:
    text = clause.lower()
    tokens = set(_TOKEN.findall(text))
    return [t.key for t in themes if any(_keyword_hit(k, text, tokens) for k in t.keywords)]


def _short(text: str, words: int) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def analyze_review(review: ReviewInput, themes: tuple[Theme, ...]) -> ReviewAnalysis:
    text = f"{review.title}. {review.body}" if review.title else review.body
    total, has_pos, has_neg = 0.0, False, False
    mentions: dict[tuple[str, str], str] = {}
    for clause, weight in _clauses(text):
        value = score_clause(clause) * weight
        total += value
        has_pos |= value >= 1.0
        has_neg |= value <= -1.0
        if abs(value) < 0.5:
            continue
        polarity = "positive" if value > 0 else "negative"
        for key in detect_themes(clause, themes):
            mentions.setdefault((key, polarity), _short(clause, 25))
    score = math.tanh(total / 4)
    if has_pos and has_neg and abs(score) < 0.6:
        label = "mixed"
    elif score >= 0.15:
        label = "positive"
    elif score <= -0.15:
        label = "negative"
    else:
        label = "neutral"
    return ReviewAnalysis(
        id=review.id,
        sentiment=label,
        score=score,
        themes=[ThemeMention(theme=k, sentiment=p, evidence=e) for (k, p), e in mentions.items()],
        summary="",  # a keyword lexicon has no idea what the main point is
    )


class LexiconAnalyzer:
    name = "lexicon"
    prompt_version = "lexicon-1"

    def __init__(self, themes: tuple[Theme, ...]):
        self.themes = themes

    async def analyze(self, reviews: list[ReviewInput]) -> BatchOutcome:
        outcome = BatchOutcome()
        for review in reviews:
            outcome.results[review.id] = analyze_review(review, self.themes)
        return outcome

def export_csv(data=None):
    if data is None:
        return None
    return data

def check_status(data=None):
    if data is None:
        return None
    return data

def export_csv(data=None):
    if data is None:
        return None
    return data

def check_status(data=None):
    if data is None:
        return None
    return data