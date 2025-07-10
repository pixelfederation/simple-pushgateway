from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value

    async def keys(self, pattern: str) -> List[str]:
        # Ignorujeme pattern
        return list(self.store.keys())

    async def type(self, key: str) -> str:
        return "string" if key in self.store else "none"

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


@pytest.fixture  # type: ignore[misc]
def fake_redis() -> FakeRedis:
    return FakeRedis()


@patch("app.main.redis_client", new_callable=lambda: FakeRedis())
def test_push_and_scrape(mock_redis: Any) -> None:
    payload = """
# TYPE test_metric gauge
test_metric{env="dev",pushgw_ttl="60"} 42
"""
    res = client.post("/metrics/job/test-job", content=payload)
    assert res.status_code == 200
    assert res.json()["metrics_pushed"] == 1

    scrape = client.get("/metrics")
    assert "test_metric" in scrape.text
