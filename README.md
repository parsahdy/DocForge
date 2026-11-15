# DocForge

A small, hands-on project for learning how to **design a multi-tenant document-ingestion platform**.

The goal is not a complete product or a high-quality RAG. The goal is to understand **how a system avoids collapsing under many large uploads**, and how it adapts to 5x and 10x growth without a rewrite. The AI part (embedding) is deliberately fake (`FakeEmbedder`) so the focus stays on engineering.

---

## Contents

1. [Problem and goals](#problem-and-goals)
2. [Architecture](#architecture)
3. [Tech stack](#tech-stack)
4. [Project layout](#project-layout)
5. [Life of a file](#life-of-a-file)
6. [Core ideas](#core-ideas)
7. [Getting started](#getting-started)
8. [API](#api)
9. [Configuration](#configuration)
10. [Experiments](#experiments)
11. [Unit tests](#unit-tests)
12. [Design docs](#design-docs)
13. [Deliberately not built](#deliberately-not-built)
14. [Known limitations](#known-limitations)

---

## Problem and goals

Tenants continuously upload Word, PDF, Excel and text files. The system extracts the text, splits it into chunks and stores them in a vector database.

**Design numbers:**

| Item | Value |
|---|---|
| Baseline rate | 5 files/s |
| Processing time per file | about 2 s |
| Growth target (no rewrite) | 5x (25/s) and 10x (50/s) |
| Small-file size limit | 25 MB |

**Questions this project answers:**

- What happens when a thousand files arrive at once? (queue, backpressure)
- What if a worker dies mid-task? (late ack, reaper)
- What if a file is permanently broken? (PermanentError, DLQ, attempt budget)
- What if a message is delivered twice? (idempotency, compare-and-set)
- What if one tenant eats all the capacity? (tenant fairness)
- What if an object exists in storage but not in the database? (reconciliation)
- What happens to old files? (lifecycle)

---

## Architecture

```
                         ┌────────────┐
 client ──(1) POST /files ─▶│  FastAPI   │──▶ PostgreSQL  (source of truth for state)
        ◀─ presigned URL ───│ (control)  │
                         └─────┬──────┘
 client ──(2) PUT file directly ─────────────▶  SeaweedFS (S3)   (data plane)
                               │
 client ──(3) POST /complete ──┘
                         │ small message (IDs only)
                         ▼
                   ┌───────────┐   small / large   ┌───────────────┐
                   │ RabbitMQ  │ ─────────────────▶│ Celery worker │
                   │ (quorum)  │                   │ parse → chunk │
                   └─────┬─────┘                   │ → embed       │
                         │ dead letters            └───┬───────┬───┘
                         ▼                             │       │
                    "dead" queue                    Qdrant    Redis
                                                  (vectors)  (tenant limits)

        Reconciler / Janitor ──▶ database + object storage + broker (maintenance loop)
```

**Architectural principles:**

- **Data plane separated from control plane.** Files go straight to object storage through a presigned URL with a signed size. The API never touches file bytes.
- **The database is the source of truth; the queue is only a doorbell.** If a message is lost, the state survives in the database and the reconciler rings the bell again.
- **Messages are small, ID-only and versioned** (`v`). Producers and consumers are deployed at different times.
- **Every pipeline stage persists its own artifact** (text, chunks), so a retry skips finished stages.
- **Every state change is a compare-and-set** (`UPDATE ... WHERE status=?`). Several workers or reconcilers can run at once without harming each other.

---

## Tech stack

| Part | Tool | Role |
|---|---|---|
| API | FastAPI | control plane, presigned URLs, file status |
| Database | PostgreSQL + SQLAlchemy + Alembic | file state (source of truth) |
| Object storage | SeaweedFS (S3 API) | original files and stage artifacts. Replaces MinIO, which was archived |
| Queue | RabbitMQ 3.13 (quorum queues) + Celery | work distribution |
| Fast state | Redis | per-tenant rate and concurrency limits |
| Vector DB | Qdrant | chunk storage with a tenant payload index |

---

## Project layout

```
docforge/
├── app/
│   ├── api/                 # HTTP layer
│   │   ├── deps.py          #   tenant resolution (X-Tenant-Id header, auth placeholder)
│   │   ├── routes_files.py  #   endpoints + admission_guard
│   │   └── schemas.py
│   ├── core/                # pure logic, no I/O
│   │   ├── config.py        #   settings and validation
│   │   ├── ids.py           #   UUIDv7 and deterministic chunk IDs
│   │   ├── states.py        #   state machine
│   │   ├── routing.py       #   size → queue
│   │   ├── messages.py      #   versioned queue message contract
│   │   ├── keys.py          #   object-storage key layout
│   │   ├── retry.py         #   exponential backoff with jitter
│   │   ├── errors.py        #   PermanentError / TransientError
│   │   └── logging.py       #   structured JSON logging
│   ├── db/                  # models, session, transition (CAS)
│   ├── services/            # adapters to the outside world
│   │   ├── storage.py       #   S3
│   │   ├── vectors.py       #   Qdrant
│   │   ├── queue.py         #   message publishing
│   │   └── tenant_limits.py #   Redis: per-tenant rate and concurrency
│   ├── pipeline/            # parse → chunk → embed
│   ├── workers/             # Celery app and task
│   ├── maintenance/         # reconciler, janitor, maintenance loop
│   └── main.py
├── migrations/              # Alembic (0001 base, 0002 reliability)
├── scripts/init_infra.py    # creates bucket, collection and the dead queue
├── tests/                   # unittest + mock
├── docs/                    # design, failure modes, scalability, roadmap
├── docker-compose.yml
├── .env.example
└── alembic.ini
```

---

## Life of a file

```
PENDING_UPLOAD ─▶ UPLOADED ─▶ QUEUED ─▶ PROCESSING ─▶ READY
                                 ▲            │
                                 │            ├─(transient error, budget left)─┐
                                 └────────────┘                                │
                                 ▲            └─(permanent error / budget gone)─▶ FAILED
                                 └──────── POST /files/{id}/retry ─────────────┘
```

1. **Register:** `POST /files` creates a `PENDING_UPLOAD` record and returns a presigned URL.
2. **Upload:** the client uploads straight to object storage.
3. **Complete:** `POST /files/{id}/complete` verifies the object and its size, moves the file to `UPLOADED` and then `QUEUED`, and publishes a message.
4. **Process:** a worker claims the file with a CAS (`QUEUED → PROCESSING`), takes a per-tenant concurrency slot and runs three stages:
   - `parse`: extracts text (`text.jsonl.gz`) after a magic-byte check.
   - `chunk`: splits the text (`chunks.<ver>.jsonl.gz`).
   - `embed`: writes chunks to Qdrant with deterministic IDs.
5. **Finish:** `READY`, or `FAILED` with an error code.

**Two crash windows in `/complete`** (two separate commits): a file left in `UPLOADED` after the first commit, and a file left in `QUEUED` whose message was never published after the second. The reconciler repairs both.

---

## Core ideas

### 1. Idempotency
- Chunk IDs are deterministic: `uuid5(tenant, file, chunker_version, index)`. Reprocessing overwrites the same points instead of duplicating them.
- The tenant is part of the ID, so two tenants uploading the same file never overwrite each other's points.
- Points from an old chunker version are removed after success with `delete_other_versions`.
- A duplicate message reaches `_claim` and is dropped when the file is not `QUEUED`.

### 2. Retry and Dead Letter Queue
- Only `PermanentError` (corrupt file, type mismatch, encrypted file) fails without retry. Every other error has an attempt budget (`MAX_ATTEMPTS`).
- **Retry is durable database state** (`QUEUED` with `retry_at`), not a broker-delayed message (native delayed delivery needs RabbitMQ 4).
- Delay: exponential backoff with full jitter, which prevents retry storms.
- `acks_late` and `reject_on_worker_lost`: if a worker dies, the message comes back.
- **DLQ:** an unprocessable message (unknown version) goes to the `dead` queue through `Reject(requeue=False)`; a looping message is cut off by `x-delivery-limit`.
- Two-level timeouts: soft (recoverable error) and hard (kills the process).

### 3. Reconciliation
The `app.maintenance.loop` process enforces these rules, all with CAS:

| Stuck state | Threshold | Action |
|---|---|---|
| `UPLOADED` | 120 s | move to `QUEUED` |
| `PROCESSING` | 1200 s (above the hard limit) | back to `QUEUED`, or `FAILED` (`WORKER_LOST`) |
| `QUEUED` and due | `retry_at` reached, or 900 s | publish the message |
| `QUEUED` but original missing | at publish time | `FAILED` with `ORIGINAL_MISSING` |

- **One publishing path:** the other rules only make a file "due"; a single function publishes everything that is due.
- **Publish first, then mark:** a crash in between yields a duplicate message (harmless), never a lost one.
- Batches are bounded so the system does not create a thundering herd of its own.
- Orphan audit (`--audit`): storage objects with no database record. Report only by default, with a grace period.

### 4. Lifecycle
- The original of a `READY` file is deleted after 7 days, of a `FAILED` file after 30 days (`original_deleted_at`).
- The extracted text artifact stays, so vectors can be rebuilt without the original.
- Abandoned uploads (`PENDING_UPLOAD`) are removed after 24 hours: object first, record second.

### 5. Tenant fairness
Three layers of defense against a heavy tenant:

1. **Admission (API):** per-tenant upload rate limit (Redis, fixed window) and a cap on files waiting or processing (counted from the database) → `429` with `Retry-After`.
2. **Execution (worker):** per-tenant concurrency cap using leases in Redis (sorted set + atomic Lua). A tenant at its cap has the file deferred with `retry_at`, without spending an attempt. Leases expire, so a dead worker cannot lock a tenant forever.
3. **Publishing (reconciler):** a `row_number` window publishes only a few files per tenant per cycle, interleaved round-robin.

All limits **fail open**: a Redis outage costs fairness, not availability.

### 6. Structural ideas
- **State machine** with explicit allowed transitions (`core/states.py`).
- **App-generated UUIDv7** and a composite key `(tenant_id, file_id)`, with keyset pagination.
- **Separate queues by size class** (`small` / `large`) behind a router function; adding a class (for example OCR) is one function plus one new worker pool.
- **Tenant isolation at every layer:** column and index in the database, a `t/{tenant}/` prefix in object storage, a mandatory filter in Qdrant.

---

## Getting started

**Requirements:** Python 3.11+, Docker and Docker Compose.

```bash
# 1) Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # includes redis

# 2) Settings
cp .env.example .env              # fill in the secrets

# 3) Infrastructure
docker compose up -d
docker compose --profile scale up -d redis-state   # Redis for tenant fairness

# 4) Database and resources
alembic upgrade head
python -m scripts.init_infra      # bucket, collection, dead queue

# 5) Services (each in its own terminal)
uvicorn app.main:app --reload
celery -A app.workers.celery_app.celery_app worker -Q small -n small@%h -c 4 --loglevel=INFO
celery -A app.workers.celery_app.celery_app worker -Q large -n large@%h -c 1 --loglevel=INFO
python -m app.maintenance.loop
```

**Important:** if you created the `small` and `large` queues earlier with different arguments, delete them in the RabbitMQ UI (`localhost:15672`, vhost `docforge`). Queue arguments cannot change after creation (`PRECONDITION_FAILED`).

---

## API

Authentication is a placeholder for now: the `X-Tenant-Id` header identifies the tenant. Replace it with real token-based auth in any real deployment.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/files` | register a file and get a presigned URL (behind `admission_guard`) |
| `POST` | `/files/{id}/complete` | announce the upload is done and enqueue it |
| `GET` | `/files/{id}` | status, `attempts`, `retry_at`, error |
| `GET` | `/files` | list with keyset pagination |
| `POST` | `/files/{id}/retry` | retry a `FAILED` file (`410` if the original was deleted) |
| `GET` | `/health` | health check |

**Notable status codes:** `429` (tenant limit, with `Retry-After`), `503` (queue unavailable; the file stays `QUEUED` and the reconciler publishes it), `409` (invalid state transition), `410` (original removed by the retention policy).

Exact request and response shapes are in `app/api/schemas.py`. Interactive docs: `http://localhost:8000/docs`

---

## Configuration

All settings come from `.env` (variable name = field name in upper case). The most important ones:

| Setting | Default | Meaning |
|---|---|---|
| `MAX_FILE_MB` | — | maximum file size |
| `SIZE_CLASS_SMALL_MAX_MB` | 25 | boundary between the `small` and `large` queues |
| `QUEUE_TYPE` | quorum | queue type (fallback: classic) |
| `MAX_ATTEMPTS` | 5 | attempt budget per file |
| `RETRY_BASE_S` / `RETRY_CAP_S` | 10 / 300 | backoff base and cap |
| `TASK_SOFT_TIME_LIMIT_S` / `TASK_HARD_TIME_LIMIT_S` | 600 / 900 | soft and hard timeouts |
| `STUCK_PROCESSING_S` | 1200 | must exceed the hard limit |
| `RECONCILE_INTERVAL_S` | 30 | maintenance loop period (also the minimum retry delay) |
| `MAINTENANCE_BATCH` | 200 | work cap per rule per cycle |
| `RETENTION_ORIGINALS_DAYS` / `RETENTION_FAILED_DAYS` | 7 / 30 | original-file retention |
| `TENANT_RATE_PER_S` | 10 | new uploads per second per tenant |
| `TENANT_MAX_BACKLOG` | 500 | files waiting or processing per tenant |
| `TENANT_MAX_CONCURRENT` | 4 | files processed at once per tenant |
| `REDIS_STATE_URL` | `redis://localhost:6379/0` | Redis address (match the compose port) |
| `EMBED_LATENCY_S` / `EMBED_ERROR_RATE` | — | simulated slowness and failures in FakeEmbedder |

Validation runs at startup (for example, `STUCK_PROCESSING_S` must exceed the hard time limit).

---

## Experiments

To see real behavior quickly, temporarily set in `.env`: `TASK_SOFT_TIME_LIMIT_S=20`, `TASK_HARD_TIME_LIMIT_S=30`, `STUCK_PROCESSING_S=60`, `RECONCILE_INTERVAL_S=10`.

1. **Worker death:** set `EMBED_LATENCY_S=5`, upload a file and `kill -9` the worker mid-task. After 60 s the reconciler moves the file back to `QUEUED` and it completes.
2. **Retry:** set `EMBED_ERROR_RATE=0.3` and upload several files. Watch `attempts` and `retry_at` in `GET /files/{id}`.
3. **Broken file:** upload a fake `.pdf` (plain text with a pdf extension). It must fail immediately with `TYPE_MISMATCH`, without retry.
4. **Poison message:** publish a message with `v: 99` to the `small` queue by hand. It must show up in the `dead` queue.
5. **Fairness:** set `TENANT_MAX_CONCURRENT=2` and `EMBED_LATENCY_S=3`. Upload 300 files as `X-Tenant-Id: big`, then immediately one file as `small`. The `small` file must become `READY` within seconds, and `big` must start receiving `429` after the cap. Look for `deferred: tenant at concurrency limit` in the logs. Stop Redis and confirm the system keeps working (fail-open).
6. **Orphan audit:** `python -m app.maintenance.loop --audit` (reports only, deletes nothing by default).

---

## Unit tests

Tests use `unittest` and mocks, so no real services are needed:

```bash
python -m unittest discover -s tests -t . -v
```

| File | What it covers |
|---|---|
| `test_core.py` | backoff, router, state machine, compare-and-set, messages, keys, IDs |
| `test_pipeline_failure.py` | `_claim` and the retry-or-fail decision |
| `test_reconciler.py` | reconciler rules with a fake database and broker |
| `test_janitor.py` | lifecycle and orphan audit |
| `test_tenant_limits.py` | the Redis limiter and how `run_job` uses it |

**What mocks cannot show:** real quorum-queue behavior, real SQL, SeaweedFS and the Lua script in Redis. Use the experiments above for those. API and Celery tests were deliberately left out because they depend on real settings at import time.

---

## Design docs

In `docs/`:

| File | Content |
|---|---|
| `design.md` | architecture, requirements, decisions (D1...) |
| `failure-modes.md` | failure modes F01-F26 and invariants |
| `scalability.md` | capacity model (Little's law), growth ladder, decisions W1-W10 |
| `roadmap.md` | build stages |

**Doc fixes still to apply:**
- `scalability.md`, row W3: the chunk ID must be `uuid5(tenant_id, file_id, chunker_version, chunk_index)`, not based on the file's sha256.
- `failure-modes.md`, row F04: describe both stuck windows (`UPLOADED` after the first commit, `QUEUED` without a message after the second).
- Document the `ix_files_status_updated` index as a deliberate exception to W1 (the reconciler scans across tenants).

---

## Deliberately not built

To keep the project short and focused on concepts:

- Kafka, Kubernetes, sharding, multi-region, cell-based architecture.
- Prometheus, Grafana and tracing (only structured JSON logs with `file_id` and `tenant_id`).
- Real authentication (only the `X-Tenant-Id` header).
- Embedding and RAG quality (the embedder is fake).
- A replay tool for the `dead` queue (users do the file-level equivalent with `POST /files/{id}/retry`).
- The outbox pattern (the reconciler takes its place).

**Signals that it is time to build some of these:** the `small` queue stays deep, worker CPU saturates, database latency rises, or several large tenants become active at once (then consider per-tenant queues or a weighted scheduler).

For the current version of DocForge, there is no need to introduce Kafka, Kubernetes, Sharding, or Multi-region architecture. However, adding Prometheus, Grafana, and Tracing in a later stage could provide significant practical value. Instead of guessing where the bottlenecks are, you can identify them using real-world metrics and data.

---

## Known limitations

- **The code has not been run.** Main risks: Celery with quorum queues on a real RabbitMQ, presigned URLs with a signed Content-Length on SeaweedFS, and the Lua script in Redis.
- **The minimum retry delay** equals `RECONCILE_INTERVAL_S` (retries live in the database, not in the broker).
- **Duplicate publishes are possible:** a file that waits longer than `STUCK_QUEUED_S` in a busy queue is published again. It is harmless (CAS) but adds load during an incident; that is why the threshold is large and the batch is small.
- **This is not true fair queueing:** messages still share one queue. A heavy tenant's impact is bounded by the backlog cap, the concurrency cap and deferral.
- **`PENDING_UPLOAD` does not count toward the backlog cap** and is bounded only by the rate limit and the 24-hour cleanup.
- **The orphan audit** lists the whole bucket and is expensive at scale; run it separately and rarely.
- **Several reconcilers at once** are safe because of CAS but duplicate work. At larger scale use `SELECT ... FOR UPDATE SKIP LOCKED`.