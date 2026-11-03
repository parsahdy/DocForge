# DocForge: System Design (Stage 0)

| | |
|---|---|
| Status | Draft v3  |
| Date | 2026-11-03 |
| Related | ADR-001 (broker), ADR-002 (object storage, to be written), failure-modes.md, scalability.md, roadmap.md |

---

## 1. Problem statement

Customers (tenants) continuously upload large documents (Word, PDF, Excel, TXT). The platform extracts text, splits it into chunks, creates embeddings and stores them for retrieval.

The business risk is **not** AI quality. The risk is the platform **collapsing when many or very large files arrive at the same time**. This document turns "must not collapse" into numbers we can design against, test against and defend.

**Second goal (v3): scale by adding resources, not by rewriting.** We build for the baseline (5 files/s), but every structural decision is made so that 5x and 10x sustained growth is reached by configuration and infrastructure changes. The reasoning, the one-way doors and the scaling triggers are in `scalability.md`.

## 2. Scope

**In scope:** upload, storage, asynchronous processing, status tracking, embedding storage, tenant fairness, overload behaviour, failure handling.

**Out of scope:** answer quality of the RAG, prompts, UI, billing, authentication provider.

**AI simplification:** embedding is done by a *fake embedder* with configurable latency, error rate and vector dimension. We want to study the behaviour of a slow, flaky dependency, not pay for a real one.

## 3. Requirements

### Functional

- FR1: a tenant can upload Word, PDF, Excel and TXT files up to 200 MB.
- FR2: a tenant can see the status of every file (`PENDING_UPLOAD → UPLOADED → QUEUED → PROCESSING → READY | FAILED`) and the reason for a failure.
- FR3: after `READY`, chunks of the file are searchable, filtered by tenant.
- FR4: a failed file can be retried without re-uploading.

### Non-functional (measurable targets are in section 7, SLOs)

- No accepted file is ever lost or silently stuck.
- The API stays fast even when the processing backlog is large.
- One tenant cannot starve the others.
- Memory per worker is bounded; one bad file cannot take down a node.
- Scalable by design: stateless tiers scale by configuration, stateful tiers have a documented scaling mechanism and trigger, and structural decisions that are expensive to reverse (tenant key, IDs, message contract) are made scale-ready from day one.

## 4. Assumptions

Every number in this document traces back to this table. When reality disagrees, we change the table first and re-derive.

| ID | Assumption | Value | Confidence / note |
|---|---|---|---|
| A1 | Average arrival rate, all tenants | 5 files/s (author's choice), growth scenario G1 | Low: is this 24/7 or a busy-hour average? Ask product. |
| A2 | Burst factor | 5x, so 25 files/s | Medium |
| A3 | File size | mean 20 MB, cap 200 MB; 80% "small" (avg 8 MB, ≤ 25 MB), 20% "large" (avg 70 MB). Check: 0.8×8 + 0.2×70 = 20.4 MB | Medium |
| A4 | Service time per file (parse + chunk + embed + store) | mean 2 s: small 1 s, large 6 s. Check: 0.8×1 + 0.2×6 = 2.0 s | Low: depends on file type and embedder. Measure in Stage 3. |
| A5 | Target worker utilisation at peak (ρ) | 0.7 | Standard headroom; see section 5.4 |
| A6 | Extracted text size | 3% of the original file, so 0.6 MB per average file | Low |
| A7 | Chunking | about 4 characters per token, about 450 effective tokens per chunk, so about 300 chunks per average file | Low |
| A8 | Embedding | 1536 dimensions, float32 = 6,144 bytes per vector; index overhead 1.3x | Configurable in the fake embedder |
| A9 | Memory per running task | small file 0.5 GB, large file 2 GB (with streaming parsers) | Low: measure |
| A10 | Duration of a burst | 10 minutes | Medium |
| A11 | Autoscaler reaction time (decision + container start) | 90 s | Medium |
| A12 | Growth scenarios (sustained averages) | G1 = 5 files/s (baseline), G5 = 25 files/s, G10 = 50 files/s | Medium: planning scenarios, not forecasts |
| A13 | Burst versus growth | Burst: short spike (A2, A10), absorbed by queue and autoscaler. Growth: sustained new baseline (A12), absorbed by capacity. The burst factor applies on top of any G. | Medium |

The three riskiest assumptions are **A1, A4 and A7**. Stage 3 measurements must replace them with real values.

## 5. Capacity model

Notation: λ = arrival rate (files/s), S = service time (s), c = concurrent task slots (one slot = one task running at a time; a worker container hosts several slots), ρ = utilisation.

### 5.1 Arrival rate and data volume

| | Average | Peak (5x) |
|---|---|---|
| Files per second | 5 | 25 |
| Files per hour | 18,000 | 90,000 (rate-equivalent) |
| Ingress (× 20 MB) | 100 MB/s ≈ 0.8 Gbit/s | 500 MB/s ≈ 4 Gbit/s |
| Object store traffic (upload + worker download) | 200 MB/s ≈ 1.6 Gbit/s | 1 GB/s ≈ 8 Gbit/s |
| Per day / per 30 days | 432,000 files / 12.96 M files | |

### 5.2 Concurrency: Little's Law

```
L = λ × S        (average number of tasks in service)
c_required = L / ρ_target
```

| Class | Share | λ avg | S | Busy slots avg | λ peak | Busy slots peak | Slots at ρ=0.7 (avg) | Slots at ρ=0.7 (peak) |
|---|---|---|---|---|---|---|---|---|
| small | 80% | 4/s | 1 s | 4 | 20/s | 20 | 6 | 29 |
| large | 20% | 1/s | 6 s | 6 | 5/s | 30 | 9 | 43 |
| **total** | | 5/s | 2 s | **10** | 25/s | **50** | **15** | **72** |

Reading the table:

- 50 slots is the **theoretical minimum** for peak (ρ = 1.0): zero headroom, queue wait grows without bound at any extra load.
- 72 slots is the **design value** (ρ = 0.7).
- The author's original estimate of 100 slots means ρ = 0.5 at peak and ρ = 0.1 at average load: safe but expensive; it is a ceiling, not a baseline.
- Large files are 20% of files but **60% of busy slots** (30 of 50) and about **86% of worker memory**. This is the numeric justification for separate small/large queues.

### 5.3 Queue dynamics when capacity is too low

Capacity of c slots is `C = c / S` files/s. If `λ > C` the backlog grows at `λ − C` files/s.

```
wait(t) ≈ t × (λ/C − 1) + S        # a file arriving t seconds after the burst started
drain_time = backlog / (C − λ_after_burst)
```

**Example:** 25 slots only (C = 12.5/s), burst of 10 minutes at λ = 25/s, ρ = 2.

- Backlog after the burst: 12.5 × 600 = 7,500 files.
- The last file of the burst waits about 600 s = 10 min.
- After the burst, the queue drains at 12.5 − 5 = 7.5/s, so 7,500 / 7.5 = 1,000 s ≈ 16.7 min.
- Total degraded period ≈ 27 min.
- SLO-2 (5 minutes) is violated for every file arriving after t = 300 / (ρ − 1) = 300 s, so about half of the burst.

**Autoscaler lag (A11):** baseline 15 slots (C = 7.5/s), burst λ = 25/s. During the 90 s before 72 slots (C = 36/s) are available the backlog grows by (25 − 7.5) × 90 = 1,575 files. After scaling it drains at 36 − 25 = 11/s, so about 143 s. The queue is the shock absorber for this lag; this is its main job.

### 5.4 Utilisation versus waiting time

Single-server intuition (M/M/1): average wait in queue = ρ / (1 − ρ) × S.

| ρ | 0.5 | 0.7 | 0.8 | 0.9 | 0.95 |
|---|---|---|---|---|---|
| wait in units of S | 1 | 2.3 | 4 | 9 | 19 |

Multi-server queues are better at the same ρ, but the curve has the same hockey-stick shape. **Never plan to run near 100%.** This is why A5 is 0.7.

### 5.5 Storage and retention

| | Value |
|---|---|
| Originals per day | 8.64 TB |
| Originals per 30 days | 259.2 TB |
| Originals per year | ≈ 3.15 PB |
| Extracted text per 30 days (0.6 MB × 12.96 M) | ≈ 7.8 TB (gzip typically 3x to 4x smaller) |

Retention options for original files:

| Policy | Originals held | Trade-off |
|---|---|---|
| Delete at `READY` | only in-flight and backlog (peak backlog 7,500 × 20 MB ≈ 150 GB) | Cheapest. No re-embedding with a new model, no replay of failures, no "download my file", Qdrant is not rebuildable. |
| Keep 7 days | ≈ 60.5 TB rolling | Covers bugs found within a week and DLQ replays. |
| Keep 30 days | ≈ 259 TB rolling | Longer safety net, high cost. |
| Keep forever | +259 TB per month | Only if the product needs it. |

**Decision (see D5):** keep originals for a limited window plus keep the extracted text, and treat "delete immediately" as a product decision, not a purely technical one.

### 5.6 Vectors and embedding load

```
vectors_per_month = files_per_month × chunks_per_file = 12.96 M × 300 ≈ 3.89 billion
raw_vector_bytes  = vectors × dim × 4 = 3.89e9 × 6,144 B ≈ 23.9 TB
with index (1.3x) ≈ 31 TB
int8 scalar quantisation ≈ 4x smaller vectors (about 6 TB), originals on disk
```

Embedding throughput:

| | Average | Peak |
|---|---|---|
| Chunks per second | 1,500 | 7,500 |
| Tokens per second (450 per chunk) | ≈ 675 k | ≈ 3.4 M |
| Tokens per minute | ≈ 40 M | ≈ 200 M |

Compare with the quota of the chosen provider. If it is lower (very likely for an external API), the quota, not the workers, decides real capacity.

### 5.7 Which resource hits the wall first (hypotheses to verify in Stage 3)

Method: for each resource compute demand ÷ capacity. The highest ratio is the bottleneck. Separate **flow** resources (rate limits, run out instantly under load) from **stock** resources (accumulate, run out over weeks).

| Resource | Type | Demand at peak | Verdict (hypothesis) |
|---|---|---|---|
| Embedding quota or throughput | flow | 7,500 chunks/s ≈ 200 M tokens/min | Likely first wall if embeddings come from an external API |
| Worker RAM | flow | ≈ 100 GB for 72 slots (29×0.5 + 43×2) | Very likely first wall on our own infrastructure (author's hypothesis) |
| Network and object store | flow | ≈ 1 GB/s combined | 8x over a 1 GbE link; borderline on 10 GbE |
| CPU | flow | up to about 50 busy cores if parsing is CPU-bound | Depends on file mix; OCR changes everything |
| Qdrant memory | stock | +31 TB per month of index | Long-term wall; needs quantisation, sharding, retention |
| Object storage capacity | stock | +259 TB per month | Depends on retention (D5) |
| PostgreSQL | flow | about 125 status writes/s (25 files × 5 transitions) | Not a limit, but watch connection count from 72+ slots |
| RabbitMQ | flow | 25 messages/s | Not a limit: throughput is irrelevant to the broker choice, delivery guarantees are |

### 5.8 Growth ladder: 1x, 5x, 10x

Same model as 5.2, applied to the sustained averages of A12. "Burst" means the 5x burst factor (A2) on top of the average.

| Metric | G1 (5/s) | G5 (25/s) | G10 (50/s) |
|---|---|---|---|
| Files per hour | 18,000 | 90,000 | 180,000 |
| Ingress (20 MB average) | 100 MB/s | 500 MB/s | 1 GB/s |
| Object store traffic (upload + worker download) | 1.6 Gbit/s | 8 Gbit/s | 16 Gbit/s |
| Busy slots (L = λ × S) | 10 | 50 | 100 |
| Slots at ρ = 0.7, average | 15 | 72 | 144 |
| Slots at ρ = 0.7, burst (5x) | 72 | 358 | 715 |
| Worker RAM at average slots (A9) | 21 GB | 100 GB | 201 GB |
| Worker RAM at burst slots | 100 GB | 502 GB | 1 TB |
| Embedding load, average | 40.5 M tokens/min | 202.5 M | 405 M |
| Database status writes (5 per file) | 25/s | 125/s | 250/s |
| Broker messages (1 per file) | 5/s | 25/s | 50/s |
| Files per month (24/7) | 13 M | 65 M | 130 M |
| Originals per month (24/7, no retention policy) | 259 TB | 1.3 PB | 2.6 PB |
| Vectors per month (300 chunks per file) | 3.9 B | 19.4 B | 38.9 B |
| Vectors, float32 with index (1.3x) | 31 TB | 155 TB | 311 TB |
| Vectors, int8 quantised (index not included) | 6 TB | 30 TB | 60 TB |

Reading the table:

- **Burst is a time problem, growth is a capacity problem.** The G1 burst ceiling (72 slots) equals the G5 average. The queue and the autoscaler absorb the first, only added capacity absorbs the second.
- **Stateless tiers scale linearly** (15, 72, 144 slots): pure configuration.
- **Shared external limits do not grow with workers.** Embedding load goes from 40 to 405 M tokens/min. If the provider quota is lower, more workers change nothing.
- **Network decides the object store.** At G10 the combined traffic is 16 Gbit/s, so a single-host store is not an option. This tier must be distributed or managed.
- **The broker is not a problem if the unit of work stays the file.** 50 messages/s is trivial. One message per chunk would be 15,000 messages/s (decision D10).
- **Stock resources dominate at scale.** 2.6 PB and 39 billion vectors per month at G10 make retention (D5), quantisation and sharding prerequisites, not optimisations. The 24/7 assumption probably overstates reality (open question 1).
- **Database writes are modest, connections are not.** 250 writes/s is fine, but 144 slots need a connection pooler.

## 6. Decisions derived from the numbers

- **D1. Provision for average, autoscale for peak, shed beyond.** Baseline 15 slots, autoscale up to 72 (hard cost ceiling 100, only raised after Stage 3 measurements). Scale on **estimated queue wait** (backlog ÷ current throughput) and oldest-message age, never on CPU. Scale out when estimated wait > 30 s; scale in only after wait < 5 s for 10 minutes (cool-down against flapping).
- **D2. Separate queues** `small` (≤ 25 MB), `large` (> 25 MB), `ocr`. Different slot counts, different memory limits (0.5 GB versus 2 GB), `prefetch = 1`.
- **D3. Backpressure threshold.** `depth_max = C_max × W_budget = 36 files/s × 120 s ≈ 4,300 files`. Above it, `POST /files` returns `429` with `Retry-After = ceil(backlog / C)`. Reject early and honestly instead of accepting work we cannot finish in time.
- **D4. Tenant limits (initial values, tune in Stage 4).** While other tenants are waiting, one tenant may hold at most 25% of the slots (18 of 72) and at most 1,000 queued files. The scheduler is work-conserving: if nobody else is waiting, a tenant may use idle capacity.
- **D5. Retention.** Keep originals 7 days after `READY` (30 days for `FAILED`), keep compressed extracted text, delete on tenant request. Open question for the stakeholder: do customers expect to download originals?
- **D6. Object storage behind the S3 API.** The application only knows "an S3-compatible endpoint". MinIO's community edition is archived and its official image is no longer published, so local development uses SeaweedFS. Write ADR-002 for this.
- **D7. Vector store planning.** Embedding dimension is configuration. Plan for scalar quantisation, on-disk vectors and sharding by tenant before the first real deployment. Not implemented in this project; documented.
- **D8. Build for 1x, design for 10x.** Every decision is classified as a one-way door (decide now, scale-ready) or a two-way door (keep simple, change later by configuration). The list is in `scalability.md`, section 2.
- **D9. Tenant key first.** `tenant_id` leads every primary key, index, object key and vector payload. It is the future partition key, shard key and the unit for cells.
- **D10. The unit of work is the file.** Queue messages are small, versioned and ID-only (`{v, file_id, tenant_id}`). A router function maps file metadata to a queue, so adding queue classes changes the router only. No per-chunk messages.
- **D11. Stage artifacts are persisted.** Original, extracted text and chunk list are each stored in object storage and passed by reference, so stages can later become separate worker pools, be retried independently, or be fanned out for large files.
- **D12. Limits are configuration.** Slots per queue, size-class threshold, backpressure threshold, tenant caps, retention and rate limits are read from environment variables, never hard-coded (see `.env.example`).
- **D13. Every scaling step has a trigger.** A scaling action is tied to a measurable threshold, for example "database connections above 70% of the maximum". Without a trigger we do not build it. See `scalability.md`, section 4.

## 7. SLOs

| ID | Indicator | Objective | Window |
|---|---|---|---|
| SLO-1 API latency | `POST /files` and `POST /files/{id}/complete`, measured at the API, including while the backlog is large | 99% < 200 ms | 30 days |
| SLO-2 Freshness | time from `/complete` to `READY`, for accepted files under load up to 5x average | 95% < 5 min, 99% < 15 min | 30 days |
| SLO-3 No silent loss | accepted files not in a terminal state (`READY` or `FAILED` with a reason) after 1 hour | 0 files (any stuck file raises an alert) | continuous |

Optional SLO-4 (fairness): a tenant below its fair share sees p95 wait within 2x of an idle system, even while another tenant floods.

Why these three: SLO-1 protects the user's first impression, SLO-2 is what the capacity model in section 5 is sized for, SLO-3 is the "do not lose files" promise from the requirements.

## 8. Verification plan (scale-down)

Laptop capacity is far below production, so we **preserve ratios (ρ), not absolutes**. Queue growth, fairness and backpressure depend on ρ, not on absolute size.

| Parameter | Production model | Lab |
|---|---|---|
| Slots | 15 to 72 | 10 fixed (4 small, 6 large) |
| Service time | 1 s / 6 s | the same (fake embedder sleeps) |
| File size | 8 MB / 70 MB | 100 KB to 5 MB for flow tests; a few 200 MB files for the memory test |
| Steady load (ρ = 0.7) | 5/s | 3.5/s |
| Overload (ρ = 2) | 25/s on 25 slots | 10/s |

Each experiment: **write a prediction first**, then measure.

1. Steady ρ = 0.7 for 10 minutes: predicted p95 wait, compare with measurement.
2. Overload ρ = 2 for 5 minutes: predicted wait(t) = t × (ρ − 1); backpressure must start at `depth_max`.
3. Fairness: one tenant floods 1,000 files while another sends 1.
4. Memory: 5 concurrent 200 MB files in containers limited to 2 GB.
5. Chaos: `kill -9` workers, restart RabbitMQ, 50% embedder errors.
6. Scaling curve: fixed overload with 1, 2, 4, 8 and 16 slots. Plot throughput and name the first shared resource where efficiency drops below 80% (`scalability.md`, section 5).
7. No-code-change test: go from 4 to 8 workers and change the size-class threshold using only environment variables.
8. Paper rehearsal of G10: before running anything, predict in writing which component breaks first at 10x and what the fix is, then compare with section 5.8.

## 9. Open questions

1. Is 5 files/s a 24/7 average or a busy-hour figure? (Changes monthly volume by about 3x.)
2. Do tenants need their original files after processing? (Drives D5 and the biggest cost line.)
3. Which embedding provider and quota, or self-hosted?
4. Are there tenant tiers (different limits)?
5. Compliance: deletion requests, data residency?
6. Which file types or archives (zip) are allowed?
7. How likely is 5x or 10x growth, and over what period? (Decides how much scale-readiness is worth paying for now.)
8. Do any tenants require isolated infrastructure, for example their own cell?

## 10. Revision log

| Version | Change |
|---|---|
| v1 | Author's first numbers: 5 files/s, 2 s per file, 5x peak, 100 workers, 259 TB per month. |
| v2 | Peak concurrency re-derived with Little's Law (50 minimum, 72 at ρ = 0.7, 100 = ρ 0.5). Wait-time formula added. Retention made explicit. Vector count and embedding load added. Bottleneck analysis split into flow and stock resources. SLOs and scale-down plan added. |
| v3 | Scalable-by-design approach: growth scenarios G1/G5/G10 (A12, A13), growth ladder (5.8), decisions D8 to D13, scaling-curve and no-code-change experiments, open questions 7 and 8. New documents: scalability.md, roadmap.md. |