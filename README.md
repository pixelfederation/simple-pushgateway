# simple-pushgateway

It stores pushed metrics in Redis with TTL and exposes them via the `/metrics` endpoint for Prometheus scraping.

---

## Features

- Supports Pushgateway API endpoints:
  - `POST /metrics/job/{job}`
  - `POST /metrics/job/{job}/instance/{instance}`
- TTL configurable via a special label `__pushgw_ttl__` (in seconds)
- Internal metrics counting number of pushed metrics and number of push requests
- Support for Redis connections with TLS and option to skip invalid certificates
- Fully configurable via environment variables

---

## Environment Variables

| Variable            | Description                                         | Default         |
|---------------------|-----------------------------------------------------|-----------------|
| `APP_PORT`          | Port to run the server on                           | `8080`          |
| `REDIS_HOST`        | Redis hostname or IP address                        | `localhost`     |
| `REDIS_PORT`        | Redis port                                          | `6379`          |
| `REDIS_DB`          | Redis database index                                | `0`             |
| `REDIS_USERNAME`    | Redis username (if auth required)                   | *(empty)*       |
| `REDIS_PASSWORD`    | Redis password (if auth required)                   | *(empty)*       |
| `REDIS_TLS`         | Enable TLS connection to Redis (`true` or `false`)  | `false`         |
| `REDIS_TLS_INSECURE`| Skip TLS certificate verification (`true` or `false`) | `false`      |

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

---

## Docker

Build and run with Docker:

```bash
docker build -t simple-pushgateway .
docker run -p 8080:8080   -e REDIS_HOST=redis-host   -e REDIS_PORT=6379   -e REDIS_TLS=false   simple-pushgateway
```

---

## Contributing

Contributions are welcome! Please follow the coding style and run tests before submitting PRs.

---

## License

MIT License