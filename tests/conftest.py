import json
from datetime import date

import httpx
import pytest
from openai import AsyncOpenAI

from demo_store.server import PLAIN_SELECTORS, create_app as create_store
from reviewdash import db
from reviewdash.config import Settings, load_themes
from reviewdash.service import Service

TODAY = date(2026, 10, 29)
STORE = "http://store.test"
SLUGS = ["aurora-anc-headphones", "ember-gooseneck-kettle", "ridgeline-trail-boots", "nimbus-robot-vacuum"]


def make_settings(**overrides) -> Settings:
    values = {"db_path": ":memory:", "analyzer": "lexicon", "request_delay_seconds": 0, "max_attempts": 3,
              "openai_api_key": "", "openai_base_url": "", "openai_model": "test-model"}
    return Settings(_env_file=None, **(values | overrides))


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def themes():
    return load_themes()


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def store():
    return create_store(today=TODAY, seed=1)


@pytest.fixture
async def http(store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=store), base_url=STORE) as client:
        yield client


class Sleeps(list):
    async def __call__(self, seconds):
        self.append(seconds)


@pytest.fixture
def sleeps():
    return Sleeps()


def add_demo_products(service: Service, slugs=SLUGS) -> list[int]:
    return [service.add_product(f"{STORE}/products/{slug}", None,
                                PLAIN_SELECTORS if slug == "ridgeline-trail-boots" else None)
            for slug in slugs]


@pytest.fixture
def service(conn, settings, http, sleeps):
    return Service(conn, settings, http_client=http, sleep=sleeps, today=TODAY)


# --- a fake OpenAI endpoint behind the real SDK -----------------------------------

def default_answer(review: dict) -> dict:
    words = review["text"].split()
    return {"id": review["id"], "sentiment": "positive", "score": 0.8,
            "themes": [{"theme": "value", "sentiment": "positive", "evidence": " ".join(words[:4])}],
            "summary": "Likes it."}


class FakeOpenAI:
    """Answers chat completions like the model would. `answer(review)` builds one entry;
    `respond(reviews, body)` can replace the whole response (return a str for raw content,
    or an httpx.Response)."""

    def __init__(self, answer=default_answer, respond=None):
        self.answer = answer
        self.respond = respond
        self.requests: list[dict] = []

    def reviews_in(self, body: dict) -> list[dict]:
        user = body["messages"][1]["content"]
        if not user.startswith("Reviews:"):
            return []
        return json.loads(user.split("\n", 1)[1])

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        reviews = self.reviews_in(body)
        if self.respond:
            result = self.respond(reviews, body)
            if isinstance(result, httpx.Response):
                return result
            content = result
        elif reviews:
            content = json.dumps({"reviews": [self.answer(r) for r in reviews]})
        else:
            content = json.dumps({"headline": "Customers love it.", "strengths": ["Sound"], "issues": ["Battery"]})
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(api_key="sk-test", base_url="http://llm.test/v1", max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


@pytest.fixture
def fake_llm():
    return FakeOpenAI()


@pytest.fixture
def llm_service(conn, http, sleeps, fake_llm):
    settings = make_settings(analyzer="openai", openai_api_key="sk-test", analyze_batch_size=4)
    return Service(conn, settings, llm_client=fake_llm.client(), http_client=http, sleep=sleeps, today=TODAY)

def rotate_secret(data=None):
    if data is None:
        return None
    return data

def compress_payload(data=None):
    if data is None:
        return None
    return data