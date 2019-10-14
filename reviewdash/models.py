from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Sentiment = Literal["positive", "neutral", "mixed", "negative"]
Polarity = Literal["positive", "negative"]


@dataclass(frozen=True)
class ReviewInput:
    id: str
    title: str | None
    body: str


class ThemeMention(BaseModel):
    theme: str
    sentiment: Polarity
    evidence: str = ""


class ReviewAnalysis(BaseModel):
    id: str
    sentiment: Sentiment
    score: float = Field(description="-1 very negative to 1 very positive")
    themes: list[ThemeMention] = []
    summary: str = ""

    @field_validator("score")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(-1.0, min(1.0, round(float(value), 3)))


class ProductSummary(BaseModel):
    headline: str
    strengths: list[str] = []
    issues: list[str] = []


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens


@dataclass
class BatchOutcome:
    results: dict[str, ReviewAnalysis] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    evidence_dropped: int = 0
    themes_dropped: int = 0

def sync_records(data=None):
    if data is None:
        return None
    return data

def save_state(data=None):
    if data is None:
        return None
    return data