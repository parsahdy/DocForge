# DocForge: Scalability Design

| | |
|---|---|
| Status | Draft v1 |
| Date | 2026-11-03 |
| Related | design.md (numbers; growth ladder in section 5.8), failure-modes.md (F20 to F26), roadmap.md |

---

## 1. What "scalable" means here

**Definition.** A system is scalable when, as load grows, we can keep the SLOs by **adding resources**, with cost growing roughly in proportion to load, and with changes limited to **configuration and infrastructure** rather than redesign or emergency data migration.

What it is **not**:

- **Not performance.** Performance is how fast one request is. A very fast single server can be completely unscalable.
- **Not peak handling.** Surviving a 10-minute burst is a buffering problem. Handling 5x more files every day is a capacity problem. They need different tools.
- **Not a technology choice.** Kubernetes, Kafka or microservices do not make a system scalable. Removing shared bottlenecks does.

### Burst versus growth

| | Burst | Growth |
|---|---|---|
| Shape | Short spike, returns to baseline | New sustained baseline |
| Absorbed by | Queue, autoscaler, backpressure | More capacity in every tier, new limits, new data volume |
| Hits first | Latency and queue wait | Stock resources: storage, vectors, connections, quotas |
| Fixed by | Buffering and shedding load | Scaling out, partitioning, retention policy |

Key observation (design.md, section 5.8): the **burst ceiling at 1x (72 slots) equals the average at 5x (72 slots)**. The worker count is identical, only the duration differs. Designing 1x with autoscaling is therefore a rehearsal for 5x.

### Vertical, horizontal, and the real difficulty

- **Vertical** scaling means a bigger machine. Simple, but it has a ceiling and a single point of failure.
- **Horizontal** scaling means more machines. Stateless tiers (API, workers) scale this way almost for free.
- **Stateful tiers** (PostgreSQL, RabbitMQ, object store, Qdrant, Redis) can only scale horizontally through **partitioning** (splitting the data) and **replication** (copying it). This is where nearly all of the design effort goes.

---

## 2. Guiding rule: build for 1x, design for 10x, do not build 10x

Every decision is either a **one-way door** (expensive to reverse, especially once data exists) or a **two-way door** (cheap to reverse). We spend design effort on one-way doors now and keep two-way doors simple. The cost of reversing a decision grows with data volume: changing a worker count takes minutes, changing the primary key of a table with a billion rows is a project.

### One-way doors: decide now

| # | Decision | Why it is hard to reverse | Our choice |
|---|---|---|---|
| W1 | Tenant key everywhere | It is the future partition and shard key. Retrofitting means rewriting all data and queries. | `tenant_id` leads every primary key and index, is the first segment of every object key, and is an indexed payload field in Qdrant. |
| W2 | ID generation | Database sequences need a single writer. Random UUIDs hurt index locality. | Application-generated, time-ordered UUIDs (UUIDv7) for `file_id`. No sequence on the hot path. |
| W3 | Deterministic chunk IDs | At scale, redelivery and retries are routine, and idempotency depends on stable IDs. | `uuid5(file_sha256, chunk_index, chunker_version)`. The embedding model version is not part of the ID (see W7). |
| W4 | Message contract | Producers and consumers deploy at different times. A fat message cannot be changed while old messages sit in the queue. | Small, versioned, ID-only: `{v, file_id, tenant_id}`. A router function maps file metadata (size class, type) to a queue. The unit of work is the **file**, never the chunk. |
| W5 | Data plane versus control plane | If bytes pass through the API, API cost grows with bytes. | The client uploads directly to object storage with a presigned URL. The API handles metadata only. |
| W6 | Persisted stage artifacts | If parse, chunk and embed run in one function that holds data in memory, they can never be split across worker pools or retried independently. | Each stage writes its output to object storage (original, then extracted text, then chunk list) and passes only a reference. |
| W7 | Versioned vector collections | Re-embedding billions of vectors in place while serving traffic is impossible. | Collection name carries a version (`chunks_v1`), and the application reads through an alias. Migration means building `v2` in the background, then switching the alias. |
| W8 | Compare-and-set state transitions | Concurrent workers corrupt state silently once parallelism is high. | `UPDATE ... WHERE id = ? AND status = ?` and check the affected row count. |
| W9 | Stateless API and workers | Local state blocks scale-out and makes failure unrecoverable. | No local file outlives a task. Temporary files live in a per-task directory that is deleted. No in-process cache is assumed to be shared. |
| W10 | Queue type | A RabbitMQ queue type is fixed at declaration. Changing it means declaring new queues, routing to them and draining the old ones. | Decide in Stage 1 whether to start with quorum queues (read the Celery documentation on quorum queue support first). |

### Two-way doors: keep simple now

Worker count, number of queues, one node of each backing service, Compose instead of Kubernetes, retention window, autoscaler logic, pool sizes, rate limit values. Each has a trigger in section 4.

---

## 3. How each component scales

| Component | Stateless? | At 1x | Scale mechanism | What limits it | Metric to watch |
|---|---|---|---|---|---|
| API | Yes | 1 to 2 replicas | More replicas behind a load balancer. Cheap because no bytes pass through. | Database connections, CPU | p99 latency, in-flight requests |
| Workers | Yes | Pools per queue | More replicas and more slots per replica | Memory (large files), embedder quota, database connections | Estimated queue wait, age of oldest message |
| RabbitMQ | No | 1 node | 3-node cluster with quorum queues, then separate queue classes or clusters | Not message rate (50 messages/s at 10x), but backlog memory and, if granularity grows, message rate | Queue depth, unacked messages, memory alarm |
| PostgreSQL | No | 1 primary | Pooler (PgBouncer), then read replicas for status reads, then time partitioning and archiving, then sharding by tenant | Connections, write amplification (5 status writes per file) | Connection usage, write latency, autovacuum lag |
| Redis | No | Cache and state instances | More memory, then Cluster for the state instance | Single-threaded hot keys | Ops/s, evicted keys, latency |
| Object storage | No | 1 node | Distributed multi-node cluster or managed S3. Only the endpoint changes. | Network (16 Gbit/s at 10x) and disk | Throughput, errors, free space |
| Qdrant | No | 1 node | Cluster with shards and replicas, one collection partitioned by a tenant payload index (not a collection per tenant), scalar quantisation, vectors on disk | RAM (3.9 billion vectors per month at 1x if 24/7) | RAM, points per node, p99 search latency |
| Embedder | External | Fake embedder | Global rate limiter, batching, cache by chunk hash, then more quota or a self-hosted pool | Provider quota, which does not grow when you add workers | Tokens/min versus quota, 429 rate |

The pattern: **stateless tiers scale by configuration, stateful tiers need a planned mechanism.** For each stateful tier above, the mechanism exists on paper before we need it.

---

## 4. The scaling ladder

Numbers are in design.md, section 5.8. Every step has a **trigger** (a measurable threshold), an **action**, and whether it needs a **code change**. If there is no trigger, we do not build it.

### Growth from 1x to 5x (5 to 25 files/s sustained)

| # | Trigger | Action | Code change | Rollback |
|---|---|---|---|---|
| 1 | Estimated queue wait above 30 s | Raise worker replicas and slots (15 to 72 slots) | No, configuration | Lower the numbers |
| 2 | Database connections above 70% of max, or pool wait p95 above 50 ms | Put PgBouncer in front of PostgreSQL | No, connection URL. Check session-level features. | Point the URL back |
| 3 | Embedder 429 rate above 1% | Global token-bucket limiter, batching, chunk-hash cache | No, configuration | Disable the cache |
| 4 | Need for high availability, or broker memory alarm | 3-node RabbitMQ cluster with quorum queues | Queue declaration only (see W10) | Keep old queues until drained |
| 5 | Qdrant RAM above 70%, or search p99 above target | Scalar quantisation, vectors on disk, 3-node cluster. The shard count is chosen when the collection is created, so plan it above the node count. | No, collection settings through the alias (W7) | Switch the alias back |
| 6 | Object store network above 60% or disk above 70% | Distributed store or managed S3, plus lifecycle rules for retention | No, endpoint | Point the endpoint back |
| 7 | API p99 above 150 ms | More API replicas | No | Fewer replicas |

### Growth from 5x to 10x (25 to 50 files/s sustained)

| # | Trigger | Action | Code change | Rollback |
|---|---|---|---|---|
| 8 | PostgreSQL write CPU above 60%, or autovacuum lag | Partition `files` and events by time, archive terminal rows, add a read replica for status reads | Migration (safe because keys are tenant-first, W1) | Keep the old table until verified |
| 9 | Broker memory or disk alarms, or publish latency above target | Separate queue classes onto separate brokers, or split by tenant tier | Router configuration (W4) | Route back |
| 10 | p99 service time of the large class above target | Fan-out and fan-in: split a large file into page-range subtasks | New pipeline stage, possible because of W6 | Disable fan-out for large files |
| 11 | One tenant above 20% of vectors, or shard skew | Shard Qdrant by tenant so a hot tenant is isolated | Collection settings (W1) | Move the tenant back |
| 12 | A single component reaches its vertical limit, or the blast radius is too large | **Cells:** shared-nothing copies of the whole stack, with a thin routing layer mapping tenant to cell | A routing layer only, because of W1 | Route tenants back to one cell |

---

## 5. Measuring scalability: the scaling curve

Scalability is a measurement, not an opinion.

**Experiment.** Keep the input above capacity so workers are always busy. Run with N = 1, 2, 4, 8, 16 slots. Record throughput X(N).

```
efficiency E(N) = X(N) / (N × X(1))      ideal = 1.0
```

Plot throughput against N. The shape tells you what limits you:

| Shape | Typical cause |
|---|---|
| Straight line | Work is independent. No shared bottleneck yet. |
| Flattens at a fixed value | A shared resource is saturated: embedder quota, database connections, broker CPU, network. |
| Bends down after a peak | Coherency cost: lock contention, retry storms, connection thrash. More workers make things worse. |

The Universal Scalability Law models it as `X(N) = N·X(1) / (1 + σ(N−1) + κ·N(N−1))`, where σ is contention (the serial fraction, as in Amdahl's law) and κ is coherency cost. We do not need to fit it. We need to read the shape, find the first bend, and name the resource that causes it.

---

## 6. Anti-patterns that silently block scaling (code review checklist)

- Auto-increment IDs on the hot path (single writer).
- Tables, indexes, object keys or payloads without a tenant key.
- State kept on a worker's local disk or in process memory.
- Messages that carry data, or one message per chunk or page.
- Read-modify-write without a conditional update.
- Global locks on the hot path. Locks per file are acceptable.
- Unbounded queues and unbounded retries (retry storms).
- One giant transaction spanning several systems.
- Autoscaling that ignores downstream capacity.
- Limits and thresholds hard-coded instead of read from configuration.
- A pipeline stage that cannot run alone because it depends on in-memory results of the previous stage.

---

## 7. What we deliberately do not build

| Not built | Why not now | Build when |
|---|---|---|
| Kafka | 50 messages/s is trivial for RabbitMQ and we do not need replay | Several independent consumers of the same stream, replay needs, or above about 10,000 messages/s sustained |
| Kubernetes | Operational cost with no benefit in a lab | More than about 3 hosts, rolling deploys in production, autoscaling on queue metrics (KEDA) |
| PostgreSQL sharding | Pooling, replicas and partitioning come first | Primary write CPU above 60% after those steps |
| Multi-region | No requirement | Data residency or regional latency requirement |
| Cells | Only the tenant-first keys are needed now (W1) | Vertical limits or blast-radius requirements, step 12 |
| Self-hosted embedding GPUs | The fake embedder and an API are enough | Quota cost exceeds GPU cost, or quota cannot be obtained |
| Per-tenant infrastructure | No tiers defined | An enterprise customer requires isolation |

Knowing what to leave out, and writing down the trigger that would bring it back, is part of the design.

---

## 8. Scalability acceptance criteria

| ID | Criterion | How we verify |
|---|---|---|
| SC1 | Doubling workers from 4 to 8 raises throughput by at least 1.7x, until the first shared bottleneck, which the report names. | Scaling curve, section 5 |
| SC2 | Killing any API or worker replica loses nothing. | Chaos test |
| SC3 | Going from 4 to 8 workers, or changing the size-class threshold, needs environment variables only. | No-code-change test |
| SC4 | Every table, index, object key and vector payload leads with `tenant_id`. | Code review checklist (section 6) |
| SC5 | The message contract is identical in the 1x and 10x scenarios. | Review of W4 |
| SC6 | Each step in section 4 has a trigger, an action and a rollback written down. | This document |