# Gridio SoC API

Read-only Python bridge from Gridio to evcc. Python 3.13+, standard library only.
Unofficial private API; use only with your own account. No charging commands are sent.

## Run

```sh
GRIDIO_DATA_DIR=./data python -m gridio_service serve
# A separate terminal (same environment and data directory):
GRIDIO_DATA_DIR=./data python -m gridio_service login request --email you@example.org
GRIDIO_DATA_DIR=./data python -m gridio_service login verify --email you@example.org
GRIDIO_DATA_DIR=./data python -m gridio_service vehicles
GRIDIO_DATA_DIR=./data python -m gridio_service status
```

The verify command prompts for the email code without putting it in process arguments.
Never point this service at a session used by an uncoordinated client or recorder.
The standalone PoC/recorder are not part of this repository or container.

## Environment configuration

| Variable | Default | Meaning |
|---|---|---|
| `GRIDIO_HOST` | `0.0.0.0` | Bind address |
| `GRIDIO_PORT` | `8080` | HTTP port |
| `GRIDIO_DATA_DIR` | `/data` | Persistent tokens, cache, locks and optional recordings |
| `GRIDIO_VEHICLE_ID` | unset | Actual Gridio device UUID; automatic selection only with exactly one vehicle |
| `GRIDIO_MIN_FETCH_INTERVAL` | `30s` | Minimum metrics request interval per vehicle, including failures; minimum 30s |
| `GRIDIO_POLL_INTERVAL` | `1h` | Background fetch interval; minimum 30s, maximum 1h |
| `GRIDIO_MAX_CACHE_AGE` | `24h` | Last successful fetch remains usable for this time during outages |
| `GRIDIO_WEB_ENABLED` | `false` | Enable login forms and status page |
| `GRIDIO_BASIC_AUTH_USERNAME` | unset | Optional HTTP Basic Auth, paired with password |
| `GRIDIO_BASIC_AUTH_PASSWORD` | unset | Protects only the optional web UI and login/status web API |
| `GRIDIO_RECORDING_ENABLED` | `false` | Record upstream metric responses and keep polling |
| `GRIDIO_RECORDING_RETENTION` | `30d` | Daily recording retention |
| `GRIDIO_LOG_LEVEL` | `INFO` | Logging level |

Duration values accept seconds or units such as `30s`, `1h`, `30d`.
Use TLS at a reverse proxy for Basic Auth. Do not expose an unprotected login UI
or vehicle data publicly. Persistent credentials are private files, not environment
variables: refresh tokens rotate. Back up and protect the whole data directory.
The 24h cache tolerance is configurable; source timestamps can legitimately be old
for parked vehicles. Cache expiry is measured from successful fetch, not vehicle time.

## HTTP and evcc

`GET /evcc/vehicles/default` selects the configured vehicle or the only vehicle.
Alternatively use `/evcc/vehicles/<actual-device-uuid>`.
Success returns JSON with `soc` (percent), `power_w` (Gridio `capacity_available`),
`status`, `source_timestamp`, `fetched_at` (Unix seconds), `available`, `cached`.
The Gridio source timestamp is not necessarily the capture time of every field.

No usable cache (including missing/failed login): HTTP **503**, plain text body
**exactly `ErrNotAvailable`**, without a newline. No invented zero values.
An auth failure does not invalidate an otherwise usable cached value.

```yaml
vehicles:
  - name: gridio
    type: custom
    title: VW via Gridio
    capacity: 77 # adapt to your battery
    soc:
      source: http
      uri: http://gridio-api:8080/evcc/vehicles/default
      jq: .soc
      cache: 60s
# At the appropriate loadpoint:
# soc:
#   poll:
#     mode: always
#     interval: 60s
```

Custom HTTP creates the getter without contacting this service, and retries on later
reads after failures. Unlike initialization-time OEM login, a missing endpoint does
not permanently prevent construction. Exact `ErrNotAvailable` handling is supported
by current evcc; validate your installed version. A wholly unreachable service still
causes ordinary connection errors. During charging evcc may query each control cycle;
our persistent upstream rate limit remains authoritative. The vehicle interface does
not expose a generic power getter: `.power_w` can be used in a separate custom meter
if appropriate, not automatically as the charger's metered power.

`GET /metrics` exposes Prometheus state without triggering upstream requests, including
authentication, data availability and last successful fetch. It remains usable without
Gridio login. `GET /health/live` never calls Gridio; use it for liveness.
`GET /health/ready` reports auth readiness, not upstream health; use an HTTP startup
that permits first-time login rather than preventing access to the login UI.

## Login UI

With `GRIDIO_WEB_ENABLED=true`, `/` contains request-code and verify-code forms;
`/login/status` shows non-secret state. Set the optional Basic Auth pair to protect it.
Vehicle selection uses `GRIDIO_VEHICLE_ID`; `vehicles` lists candidate IDs.

## Kubernetes

Build/run with a writable PVC mounted at `/data`, UID/GID 10001; set pod `fsGroup: 10001`.
The container supports a read-only root filesystem. Put ordinary environment variables
in a ConfigMap and Basic Auth credentials in a Secret. A single replica with Recreate
is recommended. Multiple processes are safe **only when sharing this data directory
on a filesystem with working POSIX flock semantics**. Independent volumes or generic
multi-host deployments are not supported. Auth, refresh, fetch and cache operations
are serialized under a stable file lock with reload after acquisition and atomic,
fsync'd state updates. Killing the process after server-side rotation but before local
persistence can still require a new login; no client can eliminate that window.

```sh
kubectl -n gridio exec -it deployment/gridio -- python -m gridio_service login request --email you@example.org
kubectl -n gridio exec -it deployment/gridio -- python -m gridio_service login verify --email you@example.org
```

These commands inherit container configuration and coordinate with the running service.
A missing Gridio login must not cause liveness restarts. Keep health probes separate
from authenticated data endpoints. A bounded hourly background fetch renews expired
access credentials as needed even without evcc traffic; server-side revocation or an
absolute session lifetime still requires login.

## Recordings

Recording does not have its own polling interval. Set `GRIDIO_POLL_INTERVAL=60s` for minutely background updates.
Enable the recorder for daily JSONL under `/data/recordings`. It shares the normal
metrics request/cache path: consumers do not produce duplicate upstream polling.
Prometheus is for monitoring; recordings preserve full decoded metrics responses and
observation times for later comparison. Tokens and authentication response bodies are
never recorded. Daily files are retained for the configured duration. Files and
checksums alone are not a tamper-proof evidence ledger.

## Container and CI

```sh
docker build -t gridio-soc-api:local .
docker run --rm -p 8080:8080 -v gridio-data:/data gridio-soc-api:local
```

GitHub Actions runs tests then builds linux/amd64 and linux/arm64 images on every
branch/tag push. GHCR package: `ghcr.io/micw/gridio-soc-api`.
Tags are `branch-<branch>`, `tag-<git-tag>` and `sha-<short-commit>` (Docker-safe
normalization). No `latest` is generated, including version tags. PRs test/build
without publishing. The workflow uses `GITHUB_TOKEN` with package write permission;
first publication/package visibility may need repository configuration.

```sh
python -m unittest discover -s tests -v
```

Tests use fake upstream transports and temporary state, never your real Gridio session.
