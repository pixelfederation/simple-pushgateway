import fnmatch
from contextlib import asynccontextmanager
from typing import AsyncIterator, Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster

from app.main import DEFAULT_TTL, _create_redis_client, _parse_ttl, app


class FakeRedis:
    store: dict[str, str]
    ttls: dict[str, int]

    def __init__(self) -> None:
        self.store = {}
        self.ttls = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def scan_iter(self, match: str | None = None) -> AsyncIterator[str]:
        for key in list(self.store.keys()):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

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


def test_push_json_single_metric(client_with_fake_redis: TestClient) -> None:
    payload = {"name": "json_metric", "value": 3.14, "labels": {"env": "dev"}}
    res = client_with_fake_redis.post("/metrics/job/test-job", json=payload)
    assert res.status_code == 200
    assert res.json()["metrics_pushed"] == 1

    scrape = client_with_fake_redis.get("/metrics")
    assert 'json_metric{env="dev",job="test-job"} 3.14' in scrape.text


def test_push_json_multiple_metrics(client_with_fake_redis: TestClient) -> None:
    payload = [
        {"name": "json_metric_one", "value": 1},
        {"name": "json_metric_two", "value": 2, "labels": {"env": "prod"}, "type": "counter"},
    ]
    res = client_with_fake_redis.post("/metrics/job/test-job/instance/server01", json=payload)
    assert res.status_code == 200
    assert res.json()["metrics_pushed"] == 2

    scrape = client_with_fake_redis.get("/metrics")
    assert 'json_metric_one{instance="server01",job="test-job"} 1.0' in scrape.text
    assert "# TYPE json_metric_two_total counter" in scrape.text


def test_push_json_ttl_label_is_stripped(client_with_fake_redis: TestClient) -> None:
    payload = {"name": "json_ttl_metric", "value": 1, "labels": {"pushgateway_ttl": "300"}}
    res = client_with_fake_redis.post("/metrics/job/test-job", json=payload)
    assert res.status_code == 200

    scrape = client_with_fake_redis.get("/metrics")
    assert "json_ttl_metric" in scrape.text
    assert "pushgateway_ttl" not in scrape.text


@pytest.mark.parametrize(
    ("ttl_raw", "expected"),
    [
        ("300", 300),
        ("45s", 45),
        ("15m", 15 * 60),
        ("3h", 3 * 3600),
        ("30d", 30 * 86400),
        ("abc", DEFAULT_TTL),
        ("30x", DEFAULT_TTL),
        ("d", DEFAULT_TTL),
        ("-5d", DEFAULT_TTL),
        ("0", DEFAULT_TTL),
    ],
)
def test_parse_ttl_formats(ttl_raw: str, expected: int) -> None:
    assert _parse_ttl({"pushgateway_ttl": ttl_raw}) == expected


def test_push_ttl_with_days_unit(client_with_fake_redis: TestClient) -> None:
    payload = {"name": "ttl_days_metric", "value": 1, "labels": {"pushgateway_ttl": "30d"}}
    res = client_with_fake_redis.post("/metrics/job/test-job", json=payload)
    assert res.status_code == 200

    fake_redis = client_with_fake_redis.app.state.redis  # type: ignore[attr-defined]
    assert list(fake_redis.ttls.values()) == [30 * 86400]


@pytest.mark.parametrize(
    "payload",
    [
        {"value": 1},  # missing name
        {"name": "no_value_metric"},  # missing value
        {"name": "bad_value_metric", "value": "not-a-number"},
        "not an object",
        [{"name": "ok_metric", "value": 1}, {"value": 2}],  # one invalid item in list
    ],
)
def test_push_json_invalid_payload(client_with_fake_redis: TestClient, payload: object) -> None:
    res = client_with_fake_redis.post("/metrics/job/test-job", json=payload)
    assert res.status_code == 400
    assert "Invalid JSON metric payload" in res.json()["detail"]


_REDIS_ENV_VARS = ["REDIS_CLUSTER", "REDIS_DB", "REDIS_USERNAME", "REDIS_PASSWORD"]


def test_create_redis_client_standalone_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _REDIS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    client = _create_redis_client()
    assert isinstance(client, Redis)
    assert not isinstance(client, RedisCluster)

    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["db"] == 0
    assert kwargs.get("username") is None
    assert kwargs.get("password") is None


def test_create_redis_client_standalone_optional_db_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_CLUSTER", raising=False)
    monkeypatch.setenv("REDIS_DB", "5")
    monkeypatch.setenv("REDIS_USERNAME", "metrics-user")
    monkeypatch.setenv("REDIS_PASSWORD", "s3cret")

    client = _create_redis_client()
    assert isinstance(client, Redis)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["db"] == 5
    assert kwargs["username"] == "metrics-user"
    assert kwargs["password"] == "s3cret"


def test_create_redis_client_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _REDIS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REDIS_CLUSTER", "true")

    client = _create_redis_client()
    assert isinstance(client, RedisCluster)
