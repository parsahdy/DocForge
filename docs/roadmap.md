# DocForge: Roadmap (scalable-by-design)

Goal: a small system, built for the baseline (5 files/s), where 5x and 10x growth is reached by configuration and infrastructure, not redesign.


## Stages

| Stage | Goal | Built (code and docs) | Your thinking work | Scalability checkpoint | Time |
|---|---|---|---|---|---|
| 0 | Numbers, failures, infrastructure | design.md, failure-modes.md, scalability.md, compose, ADR-001/002 | Growth ladder and one-way doors | Every number traces to an assumption | Day 1 morning |
| 1 | Walking skeleton with scale-ready contracts | FastAPI, tenant-first schema, UUIDv7, presigned upload, ID-only versioned messages, router, staged pipeline, fake embedder, deterministic chunk IDs, compare-and-set states | W1 to W10: why each is a one-way door | Run 1, 2, 4 workers using only an environment variable | Day 1 |
| 2 | Reliability | acks_late, error taxonomy, retry with backoff and jitter, DLQ and replay, timeouts, reconciler or outbox (ADR-003), janitor | Dual write, idempotency, retry storms | Chaos with 4 workers: nothing lost, duplicates change nothing | Day 2 morning |
| 3 | Measurement | Prometheus and Grafana basics, structured logs, load test, baseline report | Percentiles, Little's law, USE method | Baseline: where does it break first | Day 2 afternoon |
| 4 | Scale and fairness | Size-class queues, prefetch, backpressure (429), Redis limiter, per-tenant caps, autoscaling script | Head-of-line blocking, fairness, backpressure | **Scaling curve (SC1)** and the fairness experiment | Day 3 morning |
| 5 | Production-lite and 10x rehearsal | Tenant isolation test, security checklist, backup and restore test, Qdrant rebuild from artifacts, README | Blast radius, RTO, what we chose not to build | Paper rehearsal of 10x against section 5.8 | Day 3 afternoon |

Stage numbers match the "Stage" column in failure-modes.md.

## Definition of done (whole project)

- Every number in design.md traces to an assumption.
- SC1 to SC6 in scalability.md are verified or have a written reason why not.
- Chaos tests leave no file lost and no duplicate chunks.
- You can explain, for every one-way door (W1 to W10), what breaks if it is wrong.
- A short "what I would do at 10x" document exists, with triggers.