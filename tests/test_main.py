from contextlib import asynccontextmanager
from typing import AsyncIterator, Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app


class FakeRedis:
    store: dict[str, str]

    def __init__(self) -> None:
        self.store = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value

    async def keys(self, pattern: str) -> list[str]:
        return list(self.store.keys())

    async def type(self, key: str) -> str:
        return "string" if key in self.store else "none"

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


@asynccontextmanager
async def fake_lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = FakeRedis()
    yield
    app.state.redis = None


@pytest.fixture
def client_with_fake_redis() -> Generator[TestClient, None, None]:
    app.router.lifespan_context = fake_lifespan
    with TestClient(app) as c:
        yield c


def test_push_and_scrape(client_with_fake_redis: TestClient) -> None:
    payload = """
# TYPE test_metric gauge
test_metric{env="dev",pushgw_ttl="60"} 42
"""
    res = client_with_fake_redis.post("/metrics/job/test-job", content=payload)
    assert res.status_code == 200
    assert res.json()["metrics_pushed"] == 1

    scrape = client_with_fake_redis.get("/metrics")
    assert "test_metric" in scrape.text
