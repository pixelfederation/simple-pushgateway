import json
import logging
import os
import ssl
import time
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


# Helper functions
def _make_key(name: str, labels: Dict[str, str]) -> str:
    # Create Redis key with prefix and sorted labels
    label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{REDIS_METRICS_PREFIX}{name}|{label_str}"


def _parse_ttl(labels: Dict[str, str]) -> int:
    # Parse TTL from label, fallback to old label name
    ttl_raw = labels.pop("pushgateway_ttl", labels.pop("pushgw_ttl", "0"))
    try:
        ttl = int(ttl_raw)
        return ttl if ttl > 0 else DEFAULT_TTL
    except (TypeError, ValueError):
        return DEFAULT_TTL


# FastAPI app
app = FastAPI()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
    REDIS_TLS = os.getenv("REDIS_TLS", "false").lower() in ("true", "1", "yes")
    REDIS_TLS_INSECURE = os.getenv("REDIS_TLS_INSECURE", "false").lower() in (
        "true",
        "1",
        "yes",
    )

    try:
        app.state.redis = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            ssl=REDIS_TLS,
            ssl_cert_reqs=ssl.CERT_NONE if REDIS_TLS_INSECURE else ssl.CERT_REQUIRED,
            decode_responses=True,
        )

        if not await app.state.redis.ping():
            raise RuntimeError("Cannot connect to Redis server!")

        logger.info(f"✅ Connection to Redis {REDIS_HOST}:{REDIS_PORT} successful")
    except RedisError:
        logger.error("❌ Connections to Redis failed", exc_info=True)

    yield  # App runs here

    if app.state.redis:
        await app.state.redis.close()
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

    type_by_name: Dict[str, str] = {}
    count = 0

    try:
        families = list(text_string_to_metric_families(text))
    except ValueError as e:
        logger.warning("Invalid metric text format: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid metric text format: {e}",
        ) from e

    for family in families:
        type_by_name[family.name] = family.type
        for sample in family.samples:
            try:
                name, labels, value = sample.name, dict(sample.labels), sample.value
                ttl = _parse_ttl(labels)

                labels["job"] = job
                if instance:
                    labels["instance"] = instance

                key = _make_key(name, labels)
                data = {
                    "name": name,
                    "value": value,
                    "labels": labels,
                    "type": type_by_name.get(name, "gauge"),
                    "timestamp": time.time(),
                }

                await redis.setex(key, ttl, json.dumps(data))
                count += 1

            except Exception as e:
                # sem sa už môžu dostať len chyby z parsovania/serializácie
                logger.error("Error processing sample %s: %s", sample.name, e, exc_info=True)
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

    keys = await redis.keys(f"{REDIS_METRICS_PREFIX}*")
    logger.debug("Found %d keys in Redis", len(keys))

    for key in keys:
        if await redis.type(key) != "string":
            continue
        try:
            entry_raw = await redis.get(key)
            if not entry_raw:
                continue
            entry = json.loads(entry_raw)
            name = entry["name"]
            value = entry["value"]
            labels = entry["labels"]
            mtype = entry.get("type", "gauge")
            metrics_by_name[name].append((labels, value))
            type_by_name[name] = mtype

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
