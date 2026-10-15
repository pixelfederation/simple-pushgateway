import asyncio
import json
import logging
import os
import socket
import ssl
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Dict, List, Optional, Tuple, Union

import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from prometheus_client.parser import text_string_to_metric_families
from redis.asyncio.cluster import RedisCluster
from redis.exceptions import RedisError

# Global config
REDIS_METRICS_PREFIX = os.getenv("REDIS_METRICS_PREFIX", "metrics:")
DEFAULT_TTL = int(os.getenv("DEFAULT_TTL", "7200"))  # 2 hours
log_level_str = os.getenv("LOG_LEVEL", "WARNING").upper()

LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}

log_level = LOG_LEVELS.get(log_level_str, logging.WARNING)
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)


class HealthFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(HealthFilter())

# Internal metrics
push_request_counter = Counter(
    "pushgateway_push_requests_total",
    "Number of pushgateway push requests",
    ["endpoint"],
)
push_metrics_counter = Counter("pushgateway_pushed_metrics_total", "Number of metrics pushed via pushgateway")
leader_gauge = Gauge(
    "pushgateway_metrics_leader",
    "1 if this instance currently exposes the pushed metrics on /metrics, 0 otherwise",
)
leader_gauge.set(1)  # without leader election every instance exposes metrics


# Leader election Lua scripts. Both run atomically inside Redis, which closes
# the race windows a GET + SET/DEL pair would have (e.g. the lock expiring and
# being taken over by another pod between the two commands).

# Acquire the lock if it is free, or refresh its TTL if we already own it.
# KEYS[1] = lock key, ARGV[1] = our instance id, ARGV[2] = TTL in seconds.
# Returns 1 when we are the leader, 0 when someone else owns the lock.
ACQUIRE_LEADER_SCRIPT = """
local owner = redis.call("get", KEYS[1])
if owner == false then
    redis.call("set", KEYS[1], ARGV[1], "EX", ARGV[2])
    return 1
elseif owner == ARGV[1] then
    redis.call("expire", KEYS[1], ARGV[2])
    return 1
else
    return 0
end
"""

# Release the lock, but only if we still own it — never delete a lock a
# different pod acquired after ours expired.
# KEYS[1] = lock key, ARGV[1] = our instance id.
RELEASE_LEADER_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


async def _try_acquire_leadership(client: Any, key: str, instance_id: str, ttl: int) -> bool:
    return bool(await client.eval(ACQUIRE_LEADER_SCRIPT, 1, key, instance_id, str(ttl)))


async def _release_leadership(client: Any, key: str, instance_id: str) -> None:
    await client.eval(RELEASE_LEADER_SCRIPT, 1, key, instance_id)


async def _leader_loop(app: FastAPI, key: str, instance_id: str, ttl: int, interval: float) -> None:
    # Keeps (or tries to take) leadership in the background, independent of
    # Prometheus scrapes, so the lock cannot expire between scrapes.
    while True:
        await asyncio.sleep(interval)
        try:
            is_leader = await _try_acquire_leadership(app.state.redis, key, instance_id, ttl)
        except RedisError:
            logger.warning("Leader election: Redis unreachable, dropping leadership", exc_info=True)
            is_leader = False
        if is_leader != app.state.is_leader:
            logger.info(
                "Leader election: this instance (%s) is %s leader", instance_id, "now" if is_leader else "no longer"
            )
        app.state.is_leader = is_leader
        leader_gauge.set(1 if is_leader else 0)


# Helper functions
def _make_key(name: str, labels: Dict[str, str]) -> str:
    # Create Redis key with prefix and sorted labels
    label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{REDIS_METRICS_PREFIX}{name}|{label_str}"


TTL_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_ttl(labels: Dict[str, str]) -> int:
    # Parse TTL from label, fallback to old label name.
    # Accepts plain seconds ("300") or a unit suffix s/m/h/d ("30d").
    ttl_raw = str(labels.pop("pushgateway_ttl", labels.pop("pushgw_ttl", "0")))
    multiplier = 1
    if ttl_raw and ttl_raw[-1] in TTL_UNIT_SECONDS:
        multiplier = TTL_UNIT_SECONDS[ttl_raw[-1]]
        ttl_raw = ttl_raw[:-1]
    try:
        ttl = int(ttl_raw) * multiplier
        return ttl if ttl > 0 else DEFAULT_TTL
    except (TypeError, ValueError):
        return DEFAULT_TTL


def _parse_text_samples(text: str) -> List[Dict[str, Any]]:
    # Parse Prometheus text exposition format into a list of sample dicts
    try:
        families = list(text_string_to_metric_families(text))
    except ValueError as e:
        logger.warning("Invalid metric text format: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid metric text format: {e}",
        ) from e

    return [
        {
            "name": sample.name,
            "labels": dict(sample.labels),
            "value": sample.value,
            "type": family.type,
        }
        for family in families
        for sample in family.samples
    ]


def _parse_json_samples(text: str) -> List[Dict[str, Any]]:
    # Parse JSON payload: a single metric object or a list of them,
    # each {"name": str, "value": number, "labels": {str: str}, "type": str}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON metric payload: %s", e)
        raise HTTPException(status_code=400, detail=f"Invalid JSON metric payload: {e}") from e

    items = payload if isinstance(payload, list) else [payload]
    samples: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail="Invalid JSON metric payload: each metric must be an object",
            )

        name = item.get("name")
        value = item.get("value")
        labels = item.get("labels", {})
        mtype = item.get("type", "gauge")

        if not isinstance(name, str) or not name:
            raise HTTPException(
                status_code=400,
                detail="Invalid JSON metric payload: 'name' must be a non-empty string",
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON metric payload: 'value' of metric '{name}' must be a number",
            )
        if not isinstance(labels, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON metric payload: 'labels' of metric '{name}' must be an object of strings",
            )
        if not isinstance(mtype, str):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON metric payload: 'type' of metric '{name}' must be a string",
            )

        samples.append({"name": name, "labels": dict(labels), "value": value, "type": mtype})

    return samples


# FastAPI app
app = FastAPI()


def _create_redis_client() -> Union[redis.Redis, RedisCluster]:
    # Build a standalone or cluster client based on environment variables
    redis_cluster = os.getenv("REDIS_CLUSTER", "false").lower() in ("true", "1", "yes")
    redis_tls = os.getenv("REDIS_TLS", "false").lower() in ("true", "1", "yes")
    redis_tls_insecure = os.getenv("REDIS_TLS_INSECURE", "false").lower() in (
        "true",
        "1",
        "yes",
    )

    kwargs: Dict[str, Any] = {
        "host": os.getenv("REDIS_HOST", "localhost"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "ssl": redis_tls,
        "ssl_cert_reqs": ssl.CERT_NONE if redis_tls_insecure else ssl.CERT_REQUIRED,
        "decode_responses": True,
    }

    redis_username = os.getenv("REDIS_USERNAME")
    redis_password = os.getenv("REDIS_PASSWORD")
    if redis_username:
        kwargs["username"] = redis_username
    if redis_password:
        kwargs["password"] = redis_password

    if redis_cluster:
        # Redis Cluster has no database selection, REDIS_DB is ignored
        return RedisCluster(**kwargs)

    kwargs["db"] = int(os.getenv("REDIS_DB", "0"))
    return redis.Redis(**kwargs)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    leader_election = os.getenv("LEADER_ELECTION", "false").lower() in ("true", "1", "yes")
    leader_key = os.getenv("LEADER_KEY", "pushgateway:leader")
    leader_ttl = int(os.getenv("LEADER_TTL", "60"))
    leader_refresh_interval = float(os.getenv("LEADER_REFRESH_INTERVAL", "20"))
    # Unique per process, so replicas on the same host never collide
    instance_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

    app.state.is_leader = not leader_election  # without election everyone exposes metrics
    app.state.leader_task = None

    try:
        app.state.redis = _create_redis_client()

        if not await app.state.redis.ping():
            raise RuntimeError("Cannot connect to Redis server!")

        logger.info(
            "✅ Connection to Redis %s:%s successful",
            os.getenv("REDIS_HOST", "localhost"),
            os.getenv("REDIS_PORT", "6379"),
        )

        if leader_election:
            app.state.is_leader = await _try_acquire_leadership(app.state.redis, leader_key, instance_id, leader_ttl)
            leader_gauge.set(1 if app.state.is_leader else 0)
            logger.info("Leader election enabled, instance %s, leader: %s", instance_id, app.state.is_leader)
            app.state.leader_task = asyncio.create_task(
                _leader_loop(app, leader_key, instance_id, leader_ttl, leader_refresh_interval)
            )
    except RedisError:
        logger.error("❌ Connections to Redis failed", exc_info=True)

    yield  # App runs here

    if app.state.leader_task:
        app.state.leader_task.cancel()
    if app.state.redis:
        if leader_election and app.state.is_leader:
            try:
                await _release_leadership(app.state.redis, leader_key, instance_id)
                logger.info("Leader election: lock released on shutdown")
            except RedisError:
                logger.warning("Leader election: could not release lock on shutdown", exc_info=True)
        await app.state.redis.aclose()
        logger.info("🔌 Redis client closed")


app.router.lifespan_context = lifespan


# Dependency for redis client
async def get_redis(request: Request) -> redis.Redis:
    return request.app.state.redis


@app.get("/health", response_class=PlainTextResponse, include_in_schema=False)
async def health() -> PlainTextResponse:
    return PlainTextResponse("OK", media_type="text/plain")


@app.post("/metrics/job/{job}")
async def push_metrics_job(
    request: Request,
    job: str,
    redis: Annotated[redis.Redis, Depends(get_redis)],
) -> Dict[str, Any]:
    return await push_metrics(request, job, None, redis)


@app.post("/metrics/job/{job}/instance/{instance}")
async def push_metrics_instance(
    request: Request,
    job: str,
    instance: str,
    redis: Annotated[redis.Redis, Depends(get_redis)],
) -> Dict[str, Any]:
    return await push_metrics(request, job, instance, redis)


async def push_metrics(request: Request, job: str, instance: Optional[str], redis: redis.Redis) -> Dict[str, Any]:
    body = await request.body()
    text = body.decode("utf-8")

    endpoint_path = str(request.url.path)
    push_request_counter.labels(endpoint=endpoint_path).inc()

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        samples = _parse_json_samples(text)
    else:
        samples = _parse_text_samples(text)

    count = 0

    for sample in samples:
        try:
            name, labels = sample["name"], sample["labels"]
            ttl = _parse_ttl(labels)

            labels["job"] = job
            if instance:
                labels["instance"] = instance

            key = _make_key(name, labels)
            data = {
                "name": name,
                "value": sample["value"],
                "labels": labels,
                "type": sample["type"] or "gauge",
                "timestamp": time.time(),
            }

            await redis.set(key, json.dumps(data), ex=ttl)
            count += 1

        except Exception as e:
            # sem sa už môžu dostať len chyby z parsovania/serializácie
            logger.error("Error processing sample %s: %s", sample.get("name"), e, exc_info=True)
            continue

    push_metrics_counter.inc(count)
    return {"status": "ok", "metrics_pushed": count}


@app.get("/metrics")
async def get_metrics(
    request: Request,
    redis: Annotated[redis.Redis, Depends(get_redis)],
) -> Response:
    # Collect metrics from Redis
    redis_registry = CollectorRegistry()
    metrics_by_name: Dict[str, List[Tuple[Dict[str, str], Union[int, float]]]] = defaultdict(list)
    type_by_name: Dict[str, str] = {}

    # With leader election enabled, only the leader exposes the pushed metrics;
    # the other replicas serve just their own internal metrics so that
    # aggregations in Prometheus don't count the pushed values multiple times.
    if not getattr(request.app.state, "is_leader", True):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({})
        return PlainTextResponse(generate_latest(REGISTRY).decode("utf-8"), media_type="text/plain")

    # SCAN may return the same key more than once (rehashing, cluster slot
    # migration), so collect entries keyed by the Redis key — otherwise a
    # duplicate would double-count counters on the scrape output.
    entries_by_key: Dict[str, Dict[str, Any]] = {}

    async for key in redis.scan_iter(match=f"{REDIS_METRICS_PREFIX}*"):
        if key in entries_by_key:
            continue
        if await redis.type(key) != "string":
            continue
        try:
            entry_raw = await redis.get(key)
            if not entry_raw:
                continue
            entry = json.loads(entry_raw)
            entries_by_key[key] = {
                "name": entry["name"],
                "value": entry["value"],
                "labels": entry["labels"],
                "type": entry.get("type", "gauge"),
            }

            logger.debug('redis key: ""%s" value: "%s"', key, entry_raw)
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON in key %s: %s", key, e)
            continue
        except KeyError as e:
            logger.warning("Missing key %s in entry for redis key %s", e, key)
            continue
        except Exception as e:
            # catch-all, log unexpected issues but don't silently swallow them
            logger.error("Unexpected error processing key %s: %s", key, e, exc_info=True)
            continue

    for entry in entries_by_key.values():
        metrics_by_name[entry["name"]].append((entry["labels"], entry["value"]))
        type_by_name[entry["name"]] = entry["type"]

    for name, entries in metrics_by_name.items():
        label_keys = sorted({k for labels, _ in entries for k in labels})
        mtype = type_by_name.get(name, "gauge")

        try:
            if mtype == "counter":
                c = Counter(name, "Pushed counter metric", label_keys, registry=redis_registry)
                for labels, value in entries:
                    filtered = {k: labels.get(k, "") for k in label_keys}
                    c.labels(**filtered).inc(value)
            else:
                g = Gauge(name, "Pushed gauge metric", label_keys, registry=redis_registry)
                for labels, value in entries:
                    filtered = {k: labels.get(k, "") for k in label_keys}
                    g.labels(**filtered).set(value)
        except ValueError as e:
            logger.warning('Skipping metric "%s" due to ValueError: %s', name, e)
            continue

    # Join internal and redis metrics
    internal_metrics = generate_latest(REGISTRY).decode("utf-8")
    redis_metrics = generate_latest(redis_registry).decode("utf-8")

    accept_header = request.headers.get("accept", "")
    logger.debug("Client Accept header: %s", accept_header)

    if "application/json" in accept_header:
        return JSONResponse(metrics_by_name)

    return PlainTextResponse(internal_metrics + redis_metrics, media_type="text/plain")
