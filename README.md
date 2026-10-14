# simple-pushgateway

It stores pushed metrics in Redis with TTL and exposes them via the `/metrics` endpoint for Prometheus scraping.

---

## Features

- Supports Pushgateway API endpoints:
  - `POST /metrics/job/{job}`
  - `POST /metrics/job/{job}/instance/{instance}`
- Accepts both Prometheus text exposition format and JSON payloads (with `Content-Type: application/json`)
- TTL configurable per metric via a special label `pushgateway_ttl` — plain seconds (`300`) or with a unit suffix: `s`, `m`, `h`, `d` (e.g. `30d`)
- Internal metrics counting number of pushed metrics and number of push requests
- Support for Redis connections with TLS and option to skip invalid certificates
- Support for Redis Cluster (enable with `REDIS_CLUSTER=true`)
- Leader election for multi-replica deployments (enable with `LEADER_ELECTION=true`) — only one replica exposes the pushed metrics, so Prometheus aggregations don't double-count them
- Fully configurable via environment variables

---

## Environment Variables

| Variable               | Description                                         | Default         |
|------------------------|-----------------------------------------------------|-----------------|
| `APP_PORT`             | Port to run the server on                           | `8080`          |
| `REDIS_HOST`           | Redis hostname or IP address                        | `localhost`     |
| `REDIS_PORT`           | Redis port                                          | `6379`          |
| `REDIS_DB`             | Redis database index (ignored in cluster mode)      | `0`             |
| `REDIS_USERNAME`       | Redis username (optional, if auth required)         | *(empty)*       |
| `REDIS_PASSWORD`       | Redis password (optional, if auth required)         | *(empty)*       |
| `REDIS_CLUSTER`        | Connect to Redis Cluster instead of a standalone server (`true` or `false`) | `false` |
| `LEADER_ELECTION`      | Only one replica (the leader) exposes pushed metrics on `/metrics` (`true` or `false`) | `false` |
| `LEADER_KEY`           | Redis key used for the leader lock                  | `pushgateway:leader` |
| `LEADER_TTL`           | Leader lock TTL in seconds                          | `60`            |
| `LEADER_REFRESH_INTERVAL` | How often (seconds) the background task refreshes the leader lock | `20` |
| `REDIS_TLS`            | Enable TLS connection to Redis (`true` or `false`)  | `false`         |
| `REDIS_TLS_INSECURE`   | Skip TLS certificate verification (`true` or `false`) | `false`      |
| `REDIS_METRICS_PREFIX` | Prefix for keys used to store Prometheus metrics in Redis | `metrics:`      |
| `DEFAULT_TTL`          | Default TTL (seconds) for pushed metrics without a TTL label | `7200`       |
| `LOG_LEVEL`            | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) | `WARNING` |

---

## Local Development

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/simple-pushgateway.git
cd simple-pushgateway
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements-dev.txt
```

### 4. Set environment variables (example for local Redis without TLS)

```bash
export APP_PORT=8080
export REDIS_HOST=localhost
export REDIS_PORT=6379
export REDIS_DB=0
export REDIS_TLS=false
export REDIS_TLS_INSECURE=false
```

Adjust variables as needed.

### 5. Run the application

```bash
uvicorn app.main:app --host 0.0.0.0 --port $APP_PORT --reload
```

Application will be accessible at `http://localhost:8080`.

---

## Usage

- Compatible endpoints for Prometheus Pushgateway API like `/metrics/job/{job}`  
- Scrape metrics at `/metrics`

### Example usage

#### Push a single metric

```bash
echo "some_metric 3.14" | curl --data-binary @- http://localhost:8080/metrics/job/my_job
```

Response:

```json
{"status": "ok", "metrics_pushed": 1}
```

#### Push multiple metrics with a type and labels

```bash
cat <<EOF | curl --data-binary @- http://localhost:8080/metrics/job/backup/instance/server01
# TYPE backup_duration_seconds gauge
backup_duration_seconds{db="users"} 42.5
backup_duration_seconds{db="orders"} 13.2
# TYPE backup_runs_total counter
backup_runs_total 7
EOF
```

The `job` and `instance` labels are added automatically from the URL path.

#### Push a metric with a custom TTL

Set the TTL per metric using the `pushgateway_ttl` label. The value is either plain seconds (`300`) or a number with a unit suffix: `s` (seconds), `m` (minutes), `h` (hours), `d` (days). The label is stripped before the metric is stored and is not exposed on scrape:

```bash
echo 'temperature_celsius{room="kitchen",pushgateway_ttl="300"} 21.5' | \
  curl --data-binary @- http://localhost:8080/metrics/job/sensors

echo 'backup_last_success_timestamp{pushgateway_ttl="30d"} 1760400000' | \
  curl --data-binary @- http://localhost:8080/metrics/job/backup
```

The first metric expires 300 seconds after the push, the second after 30 days. Metrics without the label (or with an invalid value) use `DEFAULT_TTL` (7200 seconds by default). The legacy label name `pushgw_ttl` is also supported.

#### Push metrics as JSON

Send `Content-Type: application/json` to push metrics as JSON instead of the Prometheus text format. The body is either a single metric object or a list of them. `labels` and `type` (`gauge`/`counter`, default `gauge`) are optional; the TTL label works the same way as in the text format:

```bash
curl -H "Content-Type: application/json" -d '[
  {"name": "backup_duration_seconds", "value": 42.5, "labels": {"db": "users"}},
  {"name": "backup_runs_total", "value": 7, "type": "counter"},
  {"name": "temperature_celsius", "value": 21.5, "labels": {"room": "kitchen", "pushgateway_ttl": "300"}}
]' http://localhost:8080/metrics/job/my_job
```

Response:

```json
{"status": "ok", "metrics_pushed": 3}
```

An invalid payload (missing `name`, non-numeric `value`, …) returns `400` with a description of the problem.

#### Push from Python

The standard `prometheus_client` Pushgateway API works out of the box:

```python
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

registry = CollectorRegistry()
g = Gauge("job_last_success_timestamp", "Last successful run", registry=registry)
g.set_to_current_time()
push_to_gateway("localhost:8080", job="batch_job", registry=registry)
```

#### Scrape metrics (Prometheus format)

```bash
curl http://localhost:8080/metrics
```

```
# HELP pushgateway_push_requests_total Number of pushgateway push requests
# TYPE pushgateway_push_requests_total counter
pushgateway_push_requests_total{endpoint="/metrics/job/sensors"} 1.0
...
# HELP temperature_celsius Pushed gauge metric
# TYPE temperature_celsius gauge
temperature_celsius{job="sensors",room="kitchen"} 21.5
```

The output contains the internal pushgateway metrics followed by all pushed metrics currently stored in Redis.

#### Get metrics as JSON

Send an `Accept: application/json` header to get the stored metrics as JSON instead of the Prometheus text format:

```bash
curl -H "Accept: application/json" http://localhost:8080/metrics
```

```json
{
  "temperature_celsius": [
    [{"room": "kitchen", "job": "sensors"}, 21.5]
  ]
}
```

#### Health check

```bash
curl http://localhost:8080/health
```

### Running multiple replicas

Pushes can be load-balanced across any number of replicas (all state lives in Redis). Scraping is the problem: Prometheus scrapes every pod behind a ServiceMonitor, each pod would return the same pushed metrics under a different `instance` label, and `sum()` aggregations would count them multiple times.

Set `LEADER_ELECTION=true` to fix this. The replicas elect a leader through an atomic Redis lock (`SET NX` semantics via a Lua script): only the leader returns the pushed metrics on `/metrics`, the other replicas return just their own internal metrics. A background task refreshes the lock every `LEADER_REFRESH_INTERVAL` seconds (keep `LEADER_TTL` at 2–3× that), so leadership is stable — the lock only changes hands when the leader dies or is shut down. On graceful shutdown the leader releases the lock so a new leader takes over immediately; on a hard crash the lock expires after at most `LEADER_TTL` seconds.

Each replica exposes a `pushgateway_metrics_leader` gauge (1 on the leader, 0 elsewhere), so you can monitor and alert on the election itself. Since the pushed metrics move to a different `instance` label on failover, aggregate them with `sum by (...)` / `max by (...)` over the labels you care about rather than relying on the `instance` label.

---

## Docker

Build and run with Docker:

```bash
docker build -t simple-pushgateway .
docker run -p 8080:8080   -e REDIS_HOST=redis-host   -e REDIS_PORT=6379   -e REDIS_TLS=false   simple-pushgateway
```

### Docker Compose

`docker-compose.yaml` provides local development and test services: a standalone Redis (`localhost:6379`), a 3-node Redis Cluster (`localhost:7000-7002`), and optionally the pushgateway itself:

```bash
docker compose up -d                  # redis standalone + redis cluster
docker compose --profile app up -d    # also run the pushgateway on localhost:8080
```

---

## Tests

Unit tests run without any services:

```bash
pytest tests/test_main.py
```

End-to-end tests verify the real behavior (text/JSON push, TTL expiry, cluster scraping across all nodes) against the docker compose services. Either run them from the host:

```bash
docker compose up -d
E2E=1 pytest tests/e2e
```

or fully inside docker (useful when the compose ports are not reachable from the host):

```bash
docker compose --profile e2e run --build --rm e2e-tests
```

---

## Contributing

Contributions are welcome! Please follow the coding style and run tests before submitting PRs.

---

## License

MIT License