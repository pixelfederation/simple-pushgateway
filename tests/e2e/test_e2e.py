# End-to-end tests against real Redis services from docker-compose.yaml:
#
#   docker compose up -d
#   E2E=1 pytest tests/e2e
#
# The app runs in-process with its real lifespan (real redis clients),
# standalone against localhost:6379 and cluster against localhost:7000-7002.
import os
import time
import uuid
from typing import Generator

import pytest
import redis
from fastapi.testclient import TestClient
from redis.cluster import RedisCluster as SyncRedisCluster

from app.main import REDIS_METRICS_PREFIX, app, lifespan

pytestmark = pytest.mark.skipif(
    os.getenv("E2E") != "1",
    reason="e2e tests need docker compose services; run with E2E=1",
)

# Where the docker-compose services are reachable from. Defaults work when
# running on the docker host itself; the compose `e2e-tests` service overrides
# them for environments where localhost port mappings are not reachable.
STANDALONE_HOST = os.getenv("E2E_REDIS_HOST", "127.0.0.1")
STANDALONE_PORT = int(os.getenv("E2E_REDIS_PORT", "6379"))
CLUSTER_HOST = os.getenv("E2E_CLUSTER_HOST", "127.0.0.1")
CLUSTER_PORT = int(os.getenv("E2E_CLUSTER_PORT", "7000"))


def _require_standalone() -> redis.Redis:
    client = redis.Redis(host=STANDALONE_HOST, port=STANDALONE_PORT, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        pytest.fail(
            f"standalone Redis not reachable on {STANDALONE_HOST}:{STANDALONE_PORT} — run `docker compose up -d`"
        )
    return client


def _require_cluster() -> SyncRedisCluster:
    try:
        client = SyncRedisCluster(host=CLUSTER_HOST, port=CLUSTER_PORT, decode_responses=True)
        client.ping()
    except redis.RedisError:
        pytest.fail(f"Redis Cluster not reachable on {CLUSTER_HOST}:{CLUSTER_PORT} — run `docker compose up -d`")
    return client


def _app_client(monkeypatch: pytest.MonkeyPatch, cluster: bool) -> TestClient:
    monkeypatch.setenv("REDIS_HOST", CLUSTER_HOST if cluster else STANDALONE_HOST)
    monkeypatch.setenv("REDIS_PORT", str(CLUSTER_PORT if cluster else STANDALONE_PORT))
    monkeypatch.setenv("REDIS_CLUSTER", "true" if cluster else "false")
    for var in ("REDIS_DB", "REDIS_USERNAME", "REDIS_PASSWORD", "REDIS_TLS"):
        monkeypatch.delenv(var, raising=False)
    app.router.lifespan_context = lifespan
    return TestClient(app)


@pytest.fixture
def standalone_app(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    _require_standalone()
    with _app_client(monkeypatch, cluster=False) as client:
        yield client


@pytest.fixture
def cluster_app(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    _require_cluster()
    with _app_client(monkeypatch, cluster=True) as client:
        yield client


def _unique(name: str) -> str:
    return f"{name}_{uuid.uuid4().hex[:8]}"


def test_e2e_standalone_text_push_and_scrape(standalone_app: TestClient) -> None:
    name = _unique("e2e_text_metric")
    payload = f'{name}{{env="e2e",pushgateway_ttl="60"}} 42\n'
    res = standalone_app.post("/metrics/job/e2e-job", content=payload)
    assert res.status_code == 200
    assert res.json()["metrics_pushed"] == 1

    scrape = standalone_app.get("/metrics")
    assert f'{name}{{env="e2e",job="e2e-job"}} 42.0' in scrape.text

    # TTL label must be applied in Redis and stripped from the stored labels
    check = _require_standalone()
    keys = check.keys(f"{REDIS_METRICS_PREFIX}{name}*")
    assert len(keys) == 1
    assert 0 < check.ttl(keys[0]) <= 60
    stored = check.get(keys[0])
    assert isinstance(stored, str)
    assert "pushgateway_ttl" not in stored


def test_e2e_standalone_json_push_with_days_ttl(standalone_app: TestClient) -> None:
    name = _unique("e2e_json_metric")
    payload = [{"name": name, "value": 1.5, "labels": {"env": "e2e", "pushgateway_ttl": "30d"}}]
    res = standalone_app.post("/metrics/job/e2e-job/instance/e2e-host", json=payload)
    assert res.status_code == 200
    assert res.json()["metrics_pushed"] == 1

    scrape = standalone_app.get("/metrics")
    assert f'{name}{{env="e2e",instance="e2e-host",job="e2e-job"}} 1.5' in scrape.text

    check = _require_standalone()
    keys = check.keys(f"{REDIS_METRICS_PREFIX}{name}*")
    assert len(keys) == 1
    assert 30 * 86400 - 60 < check.ttl(keys[0]) <= 30 * 86400


def test_e2e_standalone_metric_expires(standalone_app: TestClient) -> None:
    name = _unique("e2e_expiring_metric")
    res = standalone_app.post(
        "/metrics/job/e2e-job",
        json={"name": name, "value": 1, "labels": {"pushgateway_ttl": "1"}},
    )
    assert res.status_code == 200
    assert name in standalone_app.get("/metrics").text

    time.sleep(1.5)
    assert name not in standalone_app.get("/metrics").text


def test_e2e_standalone_json_scrape(standalone_app: TestClient) -> None:
    name = _unique("e2e_json_scrape_metric")
    standalone_app.post("/metrics/job/e2e-job", json={"name": name, "value": 7})

    res = standalone_app.get("/metrics", headers={"Accept": "application/json"})
    assert res.status_code == 200
    assert res.json()[name] == [[{"job": "e2e-job"}, 7]]


def test_e2e_cluster_push_and_scrape_across_slots(cluster_app: TestClient) -> None:
    # Distinct metric names hash to different slots, so the keys land on
    # different primaries — the scrape must still return all of them
    # (verifies SCAN fans out across the whole cluster).
    names = [_unique(f"e2e_cluster_metric_{i}") for i in range(10)]
    for name in names:
        res = cluster_app.post("/metrics/job/e2e-cluster", json={"name": name, "value": 1})
        assert res.status_code == 200

    # The keys must actually be spread over more than one node
    check = _require_cluster()
    nodes = {check.cluster_keyslot(f"{REDIS_METRICS_PREFIX}{n}|job=e2e-cluster") // 5462 for n in names}
    assert len(nodes) > 1, "test metrics all hashed to one node; add more names"

    scrape = cluster_app.get("/metrics")
    for name in names:
        assert f'{name}{{job="e2e-cluster"}} 1.0' in scrape.text


def test_e2e_cluster_ttl_applied(cluster_app: TestClient) -> None:
    name = _unique("e2e_cluster_ttl_metric")
    res = cluster_app.post(
        "/metrics/job/e2e-cluster",
        json={"name": name, "value": 1, "labels": {"pushgateway_ttl": "2h"}},
    )
    assert res.status_code == 200

    check = _require_cluster()
    key = f"{REDIS_METRICS_PREFIX}{name}|job=e2e-cluster"
    assert 7200 - 60 < check.ttl(key) <= 7200


def _leader_app(monkeypatch: pytest.MonkeyPatch, leader_key: str) -> TestClient:
    monkeypatch.setenv("LEADER_ELECTION", "true")
    monkeypatch.setenv("LEADER_KEY", leader_key)
    monkeypatch.setenv("LEADER_TTL", "3")
    monkeypatch.setenv("LEADER_REFRESH_INTERVAL", "0.5")
    return _app_client(monkeypatch, cluster=False)


def test_e2e_leader_acquires_lock_and_serves_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _require_standalone()
    leader_key = f"e2e:leader:{uuid.uuid4().hex[:8]}"

    with _leader_app(monkeypatch, leader_key) as client:
        name = _unique("e2e_leader_metric")
        client.post("/metrics/job/e2e-leader", json={"name": name, "value": 1})

        scrape = client.get("/metrics")
        assert name in scrape.text
        assert "pushgateway_metrics_leader 1.0" in scrape.text

        # the lock is held in Redis with a TTL
        assert check.get(leader_key) is not None
        assert 0 < check.ttl(leader_key) <= 3


def test_e2e_non_leader_hides_metrics_and_takes_over(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _require_standalone()
    leader_key = f"e2e:leader:{uuid.uuid4().hex[:8]}"
    # another pod already holds the lock
    check.set(leader_key, "other-pod", ex=30)

    with _leader_app(monkeypatch, leader_key) as client:
        name = _unique("e2e_follower_metric")
        client.post("/metrics/job/e2e-leader", json={"name": name, "value": 1})

        scrape = client.get("/metrics")
        assert name not in scrape.text
        assert "pushgateway_metrics_leader 0.0" in scrape.text
        # pushes still work on non-leaders, data lands in Redis
        assert len(check.keys(f"{REDIS_METRICS_PREFIX}{name}*")) == 1
        # the foreign lock was not touched
        assert check.get(leader_key) == "other-pod"

        # the old leader dies: lock released -> this instance takes over
        check.delete(leader_key)
        time.sleep(1.5)  # > LEADER_REFRESH_INTERVAL

        scrape = client.get("/metrics")
        assert name in scrape.text
        assert "pushgateway_metrics_leader 1.0" in scrape.text


def test_e2e_leader_releases_lock_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _require_standalone()
    leader_key = f"e2e:leader:{uuid.uuid4().hex[:8]}"

    with _leader_app(monkeypatch, leader_key) as client:
        client.get("/metrics")
        assert check.get(leader_key) is not None

    # graceful shutdown must delete the lock so a new leader can take over immediately
    assert check.get(leader_key) is None
