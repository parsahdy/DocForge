# ADR-001: Selecting RabbitMQ as the Message Broker

* **Status:** Accepted
* **Date:** 2026-11-01
* **Decision:** Use RabbitMQ as the primary message broker.

## 1. Context

This project is a multi-tenant document processing platform designed to continuously receive and process large volumes of files.

Since file processing operations such as text extraction, chunking, and embedding generation can be time-consuming, performing these operations directly within the API request lifecycle is not an appropriate approach.

Therefore, the system requires a Message Broker to receive and manage processing tasks and distribute them asynchronously to available workers.

Two main options were considered:

* Redis
* RabbitMQ

The primary objective is to select a solution that provides reliable message delivery, queue management, and task distribution for asynchronous processing.

## 2. Options Considered

### Option A: Redis

Redis is an in-memory data store that can also be used to implement message queues.

Data structures such as Lists and Streams allow messages to be stored and distributed between producers and consumers.

**Advantages:**

* High performance for read and write operations.
* Relatively simple setup and resource requirements.
* Can be used for caching, rate limiting, and queue management.
* Redis Streams supports consumer groups and message tracking.

**Disadvantages:**

* Its messaging capabilities are not as specialized as RabbitMQ's for advanced queue management and message routing.
* Implementing certain message management and failure recovery mechanisms may require additional application-level logic.
* Using the same Redis instance for caching and queue management can introduce resource contention under heavy workloads.
* If Redis is shared across multiple system components, an outage may affect several parts of the application.

### Option B: RabbitMQ

RabbitMQ is a dedicated Message Broker designed for receiving, routing, and distributing messages between services and workers.

It supports different messaging patterns and provides several mechanisms for managing the message lifecycle.

**Advantages:**

* Supports Message Acknowledgments to confirm successful message processing.
* Supports Durable Queues and Persistent Messages to improve message durability.
* Provides flexible message routing through Exchanges and Routing Keys.
* Supports Dead Letter Exchanges for handling failed messages.
* Provides Prefetch settings to control the number of messages delivered to each worker.
* Integrates well with Celery for asynchronous task execution.

**Disadvantages:**

* Requires a separate service to be deployed and maintained.
* Configuration and operational management are more complex than a basic Redis setup.
* Introduces additional resource consumption and operational overhead.
* It is not intended to replace Redis for caching use cases.

## 3. Decision

RabbitMQ was selected as the primary message broker for document processing tasks.

The main reason is the project's need for a dedicated messaging system that provides reliable management of asynchronous tasks.

In this system, a worker may crash during file processing, a message may need to be redelivered, or a large number of tasks may be waiting in the queue simultaneously.

RabbitMQ features such as Message Acknowledgments, Durable Queues, Dead Letter Exchanges, and Prefetch help manage these situations more effectively.

Additionally, using RabbitMQ with Celery allows us to separate message queue management from task execution.

Redis will remain part of the architecture, but its responsibilities will focus on caching, rate limiting, and temporary data storage.

## 4. Consequences

### Positive Consequences

* Better separation between the API and file processing operations.
* Ability to distribute tasks across multiple workers.
* Improved management of unacknowledged messages and failed processing tasks.
* Better control over the number of messages delivered to each worker.
* Flexible task routing based on different requirements.
* Reduced dependency between queue management and caching infrastructure.
* Ability to scale the number of workers without changing the core API logic.

### Negative Consequences

* Additional infrastructure components and operational complexity.
* Requires monitoring, backup, and health management for RabbitMQ.
* Higher resource consumption compared to a basic Redis-based solution.
* Requires proper implementation of retry mechanisms and Dead Letter Queues.
* Messages may occasionally be redelivered; therefore, processing operations must be designed to be idempotent to prevent duplicate data.

## 5. Final Architecture

The responsibilities of each component are defined as follows:

| Component      | Responsibility                                              |
| -------------- | ----------------------------------------------------------- |
| FastAPI        | Receives requests and registers file information            |
| PostgreSQL     | Stores persistent metadata and document processing statuses |
| Object Storage | Stores original uploaded files                              |
| RabbitMQ       | Manages message queues and distributes tasks                |
| Celery         | Executes asynchronous tasks                                 |
| Redis          | Handles caching and rate limiting                           |
| Workers        | Perform text extraction, chunking, and embedding generation |

**Important Note:**

RabbitMQ does not guarantee exactly-once message processing by itself. Under certain conditions, messages may be delivered more than once. Therefore, idempotency must be implemented at the processing and data persistence layers.
