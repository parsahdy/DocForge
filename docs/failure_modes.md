# DocForge: Failure Modes

Purpose: list what can break, how we would **notice**, and what limits the damage. Rows F20 to F26 cover failures that appear **because we scale** (v3). A failure we cannot detect cannot be managed, so the "How we know" column matters most.

How to use: one row per failure. Add rows whenever an experiment surprises you. The "Stage" column says when the mitigation is built (S1 to S5, see the project plan).

| ID | Component | What fails | User impact | How we know | Mitigation | Stage |
|---|---|---|---|---|---|---|
| F01 | Worker | Killed mid-task (OOM, node loss, deploy) | File stuck in `PROCESSING`; partial chunks written | Unacked message returns to the queue; `PROCESSING` older than the hard limit; OOMKilled events | `acks_late` and `reject_on_worker_lost`; deterministic chunk IDs (upsert); a reaper resets stale `PROCESSING` | S2 |
| F02 | RabbitMQ | Restart or node loss | Queued messages lost if queues are not durable; workers disconnect | Broker-up gauge; consumers = 0; queue depth 0 while the DB shows `QUEUED` files | Durable queues, persistent messages, publisher confirms; fixed container hostname and a volume; reconciler re-enqueues from the DB (the DB is the source of truth) | S1, S2 |
| F03 | Client / API | Upload finished but `/complete` never called | Orphan object, row in `PENDING_UPLOAD` forever, storage leak | Count of `PENDING_UPLOAD` older than 24 h | Presigned URL expiry; janitor job deletes object and row; optional storage event to auto-complete | S2 |
| F04 | API | Crash between DB commit and queue publish (dual write) | File is `UPLOADED` but never queued: silent stuck | `UPLOADED` older than 2 min | Outbox pattern or reconciler (decide in ADR-003) | S2 |
| F05 | Pipeline | Corrupt, encrypted or unsupported file | Task fails every time | Failure count by error class | Classify permanent versus transient errors; no retry on permanent; `FAILED` with a human-readable reason | S2 |
| F06 | Queue | Poison message loops forever | Redelivery storm, CPU burn, blocked slots | Redelivered-message rate; `attempts` above the maximum | `max_retries` then Dead Letter Queue; replay tool | S2 |
| F07 | Qdrant | Unavailable or slow | Embed stage fails; files pile up in `PROCESSING` | Health probe; upsert error rate and latency | Treat as transient with backoff; keep parsed text so we never re-parse; circuit breaker; backpressure if the outage is long | S2, S4 |
| F08 | Embedding API | 429 or timeouts | Throughput collapses; naive retries amplify the problem (retry storm) | 429 rate; embed latency p95 | Global token-bucket limiter; batching; backoff with jitter; own pool and queue; cache by chunk hash | S2, S4 |
| F09 | Tenant | One tenant sends 1,000+ files | Other tenants starve behind a FIFO queue | Queue depth and wait time per tenant | Per-tenant in-flight cap, rate limit, fair dispatcher, `429` with `Retry-After` | S4 |
| F10 | Queue | Head-of-line blocking (200 MB file ahead of small files) | Small files wait minutes | Wait time by size class | Separate `small`, `large`, `ocr` queues and pools; `prefetch = 1` | S4 |
| F11 | Worker | OOM on a large file | Same as F01, may crash-loop | OOMKilled; resident memory per task | Streaming parsers; container memory limits; large queue with fewer slots and more memory; size cap checked at upload | S1, S4 |
| F12 | Pipeline | Duplicate delivery or duplicate upload | Duplicate chunks, double embedding cost | Points per `file_id` versus expected; same `sha256` processed twice | Idempotent task (check state first); deterministic point IDs; per-tenant `sha256` deduplication | S1, S2 |
| F13 | Redis | Down or evicting | Rate limits, locks and cache lost | Redis health; connection errors; evicted keys | Two instances (cache with `allkeys-lru`, state with `noeviction`); queue is on RabbitMQ so no jobs are lost; see the table below for fail-open versus fail-closed | S4 |
| F14 | Object store | Disk full or unavailable | Uploads fail even though presign succeeded; workers cannot read | Free disk %; S3 error rate | Alerts at 70% and 85%; retention lifecycle (D5); transient retry in workers | S2, S5 |
| F15 | Broker / Worker | Task runs longer than the broker's consumer timeout | Broker closes the channel and redelivers while the first run continues: two concurrent runs | Channel-closed errors; one file in two workers | Task hard time limit below the broker timeout; per-file state guard or lock | S2 |
| F16 | Pipeline | Malicious or pathological file (zip bomb, giant xlsx) | RAM and CPU exhaustion; security risk | Parse time and memory anomalies; AV verdict | Size, page and row caps; decompression ratio limit; sandboxed container with limits; magic-byte check; ClamAV | S4, S5 |
| F17 | PostgreSQL | Unavailable or out of connections | Cannot create jobs or update status | Connection count; pool wait time; error rate | Pool sizing; PgBouncer later; idempotent status updates; API returns `503` instead of hanging | S2, S4 |
| F18 | Autoscaler | Lag or flapping | SLO-2 breach at the start of a burst; cost thrash | Backlog growth versus slot count over time | Scale on estimated wait; baseline above zero; cool-down; the queue absorbs the lag | S4 |
| F19 | Qdrant | Data loss or corruption | Search returns nothing for affected files | Count of points per file versus the DB; health check | Rebuild from extracted text or originals (needs D5 retention); measure rebuild time (RTO) | S5 |
| F20 | Shared dependency | Adding workers stops helping or makes it worse (database connections, embedder quota, broker CPU, network) | Throughput flat or falling while the queue grows; cost rises | Throughput-versus-workers curve flattens; pool wait time; 429 rate | Cap replicas per dependency capacity; connection pooler; global limiter; the scaling curve test names the limit | S4 |
| F21 | Worker fleet | Thundering herd: after an outage every worker reconnects and the backlog hits all dependencies at once | Secondary outage right after recovery | Spike in connections and errors at restart | Jittered reconnect and retry, gradual ramp-up, per-dependency concurrency caps | S2, S4 |
| F22 | Tenant / partition | Hot tenant dominates one shard, partition or cache key | One partition saturated while others idle; noisy neighbour | Per-tenant and per-shard metrics (vectors, writes, latency) | Tenant-aware quotas; tenant-first keys allow moving a tenant; sharding by tenant later | S4, S5 |
| F23 | Autoscaler | Scales out beyond downstream capacity | Cost explosion and cascading failure into database or Qdrant | Replica count versus dependency utilisation | Maximum replicas derived from the weakest dependency; scale on estimated wait with a ceiling | S4 |
| F24 | Queue | Message granularity explosion (per-chunk or per-page messages) | Broker memory and CPU exhausted at moderate file rates | Messages per file; publish rate versus file rate | The unit of work is the file (D10); fan-out only through the router and only for large files | S1, S4 |
| F25 | Deploy | Mixed code versions during a rolling deploy (old workers receive new messages) | Poison messages or silent misbehaviour | Version field mismatch; error spike during deploy | Versioned message contract (`v`), backward-compatible consumers, deploy consumers before producers | S2 |
| F26 | Vector store | Re-embedding or schema change at scale (billions of vectors) | Search stale or degraded for days; rebuild saturates the cluster | Backfill progress; query latency during backfill | Versioned collections plus alias switch, throttled backfill queue, rebuild from stored text artifacts (D11) | S5 |

## Stuck-state invariants

Every non-terminal state has a maximum age. The reconciler enforces it and an alert fires when it is exceeded.

| State | Max age | Action |
|---|---|---|
| `PENDING_UPLOAD` | 24 h | Expire, delete object and row |
| `UPLOADED` | 2 min | Re-enqueue (F04) |
| `QUEUED` | expected wait + margin (start with 15 min) | Alert; check queue and consumers |
| `PROCESSING` | task hard limit + 5 min | Return to `QUEUED` with `attempts + 1`, or `FAILED` after the maximum |

## Fail-open versus fail-closed decisions

| Dependency down | Choice | Reason |
|---|---|---|
| Rate limiter (Redis) | Fail open with a coarse local limit | Redis must not block all uploads; the global backpressure threshold (D3) still protects the system |
| Distributed lock (Redis) | Fail closed: retry later | Idempotency protects correctness, a lock only avoids wasted work |
| Cache (Redis) | Treat as a miss | A cache is an optimisation only |
| Vector store (Qdrant) | Retry, then keep the file in a retryable state | Do not mark files `FAILED` for an infrastructure outage |

## Not yet covered (add as experiments reveal them)

- Clock skew between services.
- Partial network partitions (API reaches the DB but not the broker).
- Schema migration while workers run.
- Credential rotation for the object store.