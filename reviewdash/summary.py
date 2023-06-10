"""Product summary without an LLM: the same facts the model gets, turned into sentences by a template."""

from .models import ProductSummary


def template_summary(product_name: str, facts: dict) -> ProductSummary:
    counts = facts["sentiment_counts"]
    analyzed = sum(counts.values())
    if not analyzed:
        return ProductSummary(headline=f"No analyzed reviews for {product_name} yet.")
    positive_pct = round(counts["positive"] / analyzed * 100)
    negative_pct = round(counts["negative"] / analyzed * 100)
    themes = facts["themes"]
    praised = sorted((t for t in themes if t["positive"]), key=lambda t: -t["positive"])
    criticized = sorted((t for t in themes if t["negative"]), key=lambda t: -t["negative"])

    headline = f"{positive_pct}% of {analyzed} reviews are positive and {negative_pct}% negative"
    if praised and criticized and praised[0]["theme"] == criticized[0]["theme"]:
        headline += f"; opinion is split on {praised[0]['label'].lower()}."
    elif praised and criticized:
        headline += f"; most praised: {praised[0]['label'].lower()}, most criticized: {criticized[0]['label'].lower()}."
    elif praised:
        headline += f"; most praised: {praised[0]['label'].lower()}."
    else:
        headline += "."

    strengths = [f"{t['label']}: {t['positive']} positive vs {t['negative']} negative mentions"
                 for t in praised if t["positive"] > t["negative"]][:3]
    issues = []
    for trend in facts.get("trends_last_90_days", []):
        if trend["sentiment"] == "negative":
            issues.append(f"{trend['theme']} complaints rising: {trend['last_90_days']} in the last 90 days "
                          f"vs {trend['previous_90_days']} in the 90 before")
    issues += [f"{t['label']}: {t['negative']} negative mentions" for t in criticized
               if t["negative"] >= t["positive"] and not any(i.startswith(t["label"]) for i in issues)]
    if facts.get("rating_text_mismatches"):
        issues.append(f"{facts['rating_text_mismatches']} reviews where the star rating contradicts the text")
    return ProductSummary(headline=headline, strengths=strengths, issues=issues[:3])

def subscribe_topic(data=None):
    if data is None:
        return None
    return data

def send_notification(data=None):
    if data is None:
        return None
    return data

def subscribe_topic(data=None):
    if data is None:
        return None
    return data