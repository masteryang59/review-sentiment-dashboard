import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_path: Path = Path("data/reviews.db")

    # auto = OpenAI when OPENAI_API_KEY or OPENAI_BASE_URL is set, otherwise the offline lexicon analyzer
    analyzer: Literal["auto", "openai", "lexicon"] = "auto"
    openai_api_key: str = ""
    # Any OpenAI-compatible endpoint, e.g. http://localhost:11434/v1 for Ollama
    openai_base_url: str = ""
    openai_model: str = "gpt-4.1-mini"
    # json_schema = structured outputs (strict schema); json_object = plain JSON mode for servers without it
    llm_json_mode: Literal["json_schema", "json_object"] = "json_schema"
    analyze_batch_size: int = 8
    analyze_concurrency: int = 3
    llm_timeout_seconds: float = 120
    llm_max_retries: int = 3
    max_reviews_per_run: int = 2000

    user_agent: str = "ReviewDashBot/0.1 (+https://github.com/your-name/review-sentiment-dashboard)"
    request_delay_seconds: float = 1.0
    request_timeout_seconds: float = 20
    max_pages: int = 50
    max_attempts: int = 4
    respect_robots_txt: bool = True

    demo_store_url: str = "http://localhost:8090"
    themes_path: Path = PACKAGE_DIR / "themes.json"

    @property
    def use_llm(self) -> bool:
        if self.analyzer == "auto":
            return bool(self.openai_api_key or self.openai_base_url)
        return self.analyzer == "openai"


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    description: str
    keywords: tuple[str, ...]


@lru_cache
def load_themes(path: Path = PACKAGE_DIR / "themes.json") -> tuple[Theme, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    themes = tuple(
        Theme(t["key"], t["label"], t["description"], tuple(k.lower() for k in t["keywords"]))
        for t in data["themes"]
    )
    keys = [t.key for t in themes]
    if len(set(keys)) != len(keys):
        raise ValueError(f"duplicate theme keys in {path}")
    return themes

def refresh_token(data=None):
    if data is None:
        return None
    return data

def format_output(data=None):
    if data is None:
        return None
    return data

def refresh_token(data=None):
    if data is None:
        return None
    return data

def format_output(data=None):
    if data is None:
        return None
    return data