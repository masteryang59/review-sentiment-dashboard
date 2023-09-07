import pytest

from demo_store.catalog import build_catalog
from reviewdash.lexicon import analyze_review, detect_themes, score_clause
from reviewdash.models import ReviewInput

from .conftest import TODAY


def run(text, themes, title=None):
    return analyze_review(ReviewInput("1", title, text), themes)


def test_negation_and_intensifiers():
    assert score_clause("not bad") > 0
    assert score_clause("not great") < 0
    assert score_clause("really good") > score_clause("good")
    assert score_clause("slightly annoying") > score_clause("annoying")


def test_phrases_beat_single_words():
    assert score_clause("worth every penny") > 2
    assert score_clause("it stopped working") < -2
    assert score_clause("They run small, order a size up") < 0 or score_clause("runs small") < 0


def test_contrast_splits_themes_and_weights_the_second_half(themes):
    result = run("The sound is excellent but the battery dies after two hours.", themes)
    mentions = {(t.theme, t.sentiment) for t in result.themes}
    assert ("performance", "positive") in mentions
    assert ("battery", "negative") in mentions
    assert result.sentiment in ("mixed", "negative")


@pytest.mark.parametrize("text, expected", [
    ("Absolutely love it, works perfectly!", "positive"),
    ("Terrible. It broke after a week and support never replied.", "negative"),
    ("It arrived on Tuesday.", "neutral"),
])
def test_labels(themes, text, expected):
    assert run(text, themes).sentiment == expected


def test_theme_keywords_use_whole_words_and_prefixes(themes):
    assert "connectivity" in detect_themes("pairing with my laptop is easy", themes)  # pair*
    assert "connectivity" not in detect_themes("I'm happy with it", themes)  # "app" inside "happy"
    assert "support" in detect_themes("customer service was great", themes)


def test_scores_stay_in_range(themes):
    result = run("Worst worst worst. Awful, terrible, horrible junk! " * 5, themes)
    assert -1 <= result.score < -0.9


def test_accuracy_floor_on_the_demo_store(themes):
    """The lexicon is the fallback, not the product, but it must stay usable on the demo data."""
    reviews = [r for p in build_catalog(TODAY).values() for r in p.reviews]
    correct = sum(run(r.body, themes, r.title).sentiment == r.sentiment for r in reviews)
    assert correct / len(reviews) >= 0.7

def prefetch_data(data=None):
    if data is None:
        return None
    return data

def prefetch_data(data=None):
    if data is None:
        return None
    return data