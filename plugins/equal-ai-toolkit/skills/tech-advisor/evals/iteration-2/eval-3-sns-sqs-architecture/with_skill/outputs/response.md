# SNS/SQS Event-Driven Architecture: Deep-Dive Analysis

So I've dug through every service's event publishing and consumption code, the CDK infrastructure definitions, the architecture docs, and cross-checked everything against the actual implementations. Here's what I'm seeing -- there's a lot of good foundational work here, but also some significant inconsistencies and scale concerns that we need to talk through.

---

## Doc-Code Discrepancies

Before we dive into the architecture itself, I found several places where docs and code disagree. These are high-signal findings.

**1. Missing SNS filter policies -- docs imply selective routing, CDK delivers everything everywhere.**

The PPS architecture doc (`myequal-post-processing-service/docs/architecture/LLD-event-system.md`) describes the system as if PPS only receives `call_initiated`, `call_completed`, and `recording_saved` events. The user-services doc describes receiving only `processing_completed`. But looking at the actual CDK infrastructure (`equalai/stacks/common/equalai-events-stack.ts`, lines 116-155), *none of the SNS-to-SQS subscriptions have filter policies*. Every subscriber gets every event type. The CDK constructs support `filterPolicy` (confirmed in `commons-lib/src/constructs/sns-sqs-subscription-construct.ts`, line 13), but it's never used.

This means:
- PPS receives its own `processing_completed` events and has to discard them in code (confirmed: `_DISCARDABLE_EVENT_TYPES = frozenset({"processing_completed", "recording_saved"})` in `simple_sqs_consumer.py` line 45).
- Memory service receives `call_initiated`, `call_completed`, `recording_saved` events it doesn't care about -- it deletes unknown types silently.
- User-services receives `call_initiated`, `call_completed`, `recording_saved` events it published itself.
- Evaluations service receives everything too.

This is wasting SQS receive costs, increasing consumer load, and adding unnecessary code complexity for self-skip logic.

**2. User-services SNS publisher: Two different implementations coexist.**

The user-services has two completely different SNS publisher implementations:
- `app/event_publishers/aws_sns.py` -- Uses synchronous `boto3` with `asyncio.to_thread()`, single client with connection pooling, `botocore.Config` for retry/timeout settings, singleton factory pattern.
- `app/event_publishers/base.py` -- Abstract base with `AbstractEventPublisher` that the `__init__.py` wraps around the above via `SNSEventPublisherWrapper`.

Meanwhile, the backend service (`myequal-ai-backend/backend/event_publishers/aws_sns.py`) uses `aioboto3` (native async) instead of `boto3 + asyncio.to_thread()`. These are fundamentally different concurrency approaches for the same operation.

**3. Backend service BaseEvent source is wrong.**

In `myequal-ai-backend/backend/event_publishers/base.py` line 33, the default source is `"myequal-ai-user-services"` even though this is the backend service. This means call_completed events published by the backend incorrectly claim to originate from user-services.

**4. CloudEvent ID generation is inconsistent across services.**

- Backend: Uses `uuid.uuid4()` per event (good -- globally unique).
- User-services: Uses `f"{event_type}-{datetime.now().timestamp()}"` (dangerous -- two events of the same type at the same millisecond will collide).
- Memory-service: Uses `f"{event_type}-{uuid.uuid4()}"` (fine -- unique).
- PPS: Uses the CloudEvent `id` from the event object.

The user-services approach is a deduplication/idempotency risk, especially under burst load.

---

## Current State: How It Actually Works

### The Topology

The architecture is a **two-topic fan-out** model:

**Topic 1: `call-events`** (the main event bus)
- **Publishers**: Backend service (call_completed), User-services (recording_saved, call_initiated), PPS (processing_completed)
- **Subscribers** (all via SQS): PPS queue, User-services queue, Memory-service queue, Evaluations queue, Vocab-extraction queue

**Topic 2: `memory-publish-events`** (reverse flow)
- **Publisher**: Memory service (caller.data.updated)
- **Subscriber**: User-services via dedicated queue

Each SQS queue has a DLQ with `maxReceiveCount=5` (3 for the memory-publish queue), 14-day retention, and 120-second visibility timeout (180s for vocab-extraction).

### Publishing Pattern

All services use fire-and-forget publishing with CloudEvents envelope format. The pattern across services:

1. Create CloudEvent envelope (specversion 1.0, type, source, data)
2. Serialize to JSON
3. Publish to SNS with message attributes (eventType, source, DelaySeconds)
4. Background task -- main request path is never blocked

User-services and memory-service use synchronous `boto3` wrapped in `asyncio.to_thread()` with singleton factory pattern and connection pooling via `botocore.Config`. Backend uses `aioboto3` natively. PPS uses `boto3` with `loop.run_in_executor()`.

### Consumption Pattern

Each service has its own consumer implementation. All use long polling (20s wait), batch receive (up to 10), and semaphore-based concurrency control:

- **PPS** (`SimpleSQSConsumer`): Most mature. Dual worker pools (call_initiated vs call_completed), Redis-based idempotency, backpressure detection, signal handling, exponential backoff with jitter, per-pool capacity monitoring. Uses `aioboto3`.
- **User-services** (`SQSEventConsumer`): Singleton pattern, connection pooling via `botocore.Config`, comprehensive Datadog metrics. Uses `boto3 + asyncio.to_thread()`.
- **Memory-service** (`SQSEventConsumer`): Uses `aioboto3`, semaphore-based concurrency, Datadog tracing throughout. Normalizes all events to legacy "EventGrid" internal format (code artifact, not an Azure dependency).

---

## External Context: Best Practices Assessment

### What you're doing well

1. **CloudEvents format** -- Using CloudEvents 1.0 spec is a solid choice for event envelope standardization. This is the CNCF standard and gives you interoperability.

2. **SNS fan-out to SQS** -- This is the canonical AWS pattern for event-driven architectures. SNS provides pub/sub decoupling, SQS provides reliable consumption with at-least-once delivery.

3. **DLQs on every queue** -- Good. Every queue has a dead-letter queue configured via CDK. This is essential for handling poison messages.

4. **Raw message delivery** -- All subscriptions use `rawMessageDelivery: true`, which means consumers get the CloudEvent directly without the SNS wrapping envelope. This is correct and simplifies parsing.

5. **Long polling** -- All consumers use `WaitTimeSeconds=20`, which is the maximum and most cost-effective approach.

6. **Connection pooling** -- User-services and memory-service properly configure `botocore.Config` with `max_pool_connections` for boto3 client reuse.

7. **Fire-and-forget publishing** -- Publishing never blocks the request path. This is critical for latency-sensitive operations like call handling.

8. **Idempotency in PPS** -- Redis-based idempotency with lock acquisition/release is the right approach for preventing duplicate processing.

### What could be significantly better

Here's where I'd push hard in a design review.

---

## My Take: The Five Biggest Issues

### 1. No SNS Filter Policies = Wasted Cost and Complexity

This is the most impactful quick win. Every SQS queue receives every event type published to the call-events topic. At 10x load, this means:

- PPS processes (and immediately discards) every `processing_completed` it publishes itself, plus every `recording_saved` event. That's roughly 2x the messages it needs.
- Memory service receives and discards `call_initiated`, `call_completed`, `recording_saved` events. It only cares about `processing_completed`.
- User-services receives its own `call_initiated` and `recording_saved` events back.

With filter policies on the SNS subscriptions, each queue only receives relevant events. The CDK construct already supports it. The fix is adding `filterPolicy` to each `SnsSqsSubscriptionConstruct.build()` call in `equalai-events-stack.ts`:

```typescript
// Example: PPS only gets call_initiated and call_completed
filterPolicy: {
    eventType: sns.SubscriptionFilter.stringFilter({
        allowlist: ['call_initiated', 'call_completed']
    })
}
```

At scale, this reduces SQS receive/process overhead by 50-70% per service and eliminates all the self-skip guard code.

### 2. Three Different boto3 Patterns Across Four Services

You have three fundamentally different approaches to async AWS SDK usage:

| Service | SDK | Pattern | Connection Pooling |
|---|---|---|---|
| Backend | `aioboto3` | Native async context manager per call | No reuse -- new client per publish |
| User-services | `boto3` | `asyncio.to_thread()` + singleton client | Yes -- single client reused |
| Memory-service | `aioboto3` | Session-based, new client per poll cycle | Partial -- session reused, client recreated |
| PPS publisher | `boto3` | `loop.run_in_executor()` + singleton client | Yes -- single client reused |
| PPS consumer | `aioboto3` | Session-based, new client per receive | No reuse -- new client context per call |

The backend's `aioboto3` approach (`async with await self._get_client() as sns:`) creates a new client context on every publish. Under load, this means new HTTP connections for every SNS publish. This is the worst-performing option.

The user-services approach (boto3 singleton + `asyncio.to_thread()`) is actually the best pattern for production: one client, one connection pool, non-blocking I/O via thread offloading. It's the approach AWS themselves recommend for Python async applications when connection reuse matters.

The PPS consumer creates a new aioboto3 client context (`async with self.session.client("sqs") as sqs:`) on every `_receive_messages()` call (line 529 of `simple_sqs_consumer.py`) and every `_delete_message()` call. Under high throughput, this means constant client creation/destruction during the hot loop.

**Recommendation**: Standardize on the user-services pattern (`boto3` singleton + `asyncio.to_thread()`) everywhere. Create a shared library or at minimum document the canonical pattern.

### 3. No Backpressure Propagation from Consumer to Infrastructure

PPS has the best backpressure implementation: when worker pools hit 2x capacity, it pauses receiving. But the other services don't have this. More critically, none of the services have circuit breakers or health-aware consumption patterns.

Consider this scenario: the database goes slow (not down, just slow). Memory service's `CallerMemoryCreator.execute()` makes LLM calls that take 10-15 seconds each. The visibility timeout is 120 seconds. If each message takes 15 seconds and you have 10 concurrent tasks, you process ~0.67 messages/second. If messages arrive faster than that, the queue grows unbounded, visibility timeouts start expiring, messages get redelivered, and you enter a retry storm where the same messages are processed multiple times.

The DLQ `maxReceiveCount=5` helps eventually, but you'll burn through those 5 retries during the degraded period. This is a classic cascading failure in event-driven systems.

**What's missing**:
- Adaptive visibility timeout extension (heartbeat pattern) for long-running processors
- Queue depth monitoring with CloudWatch alarms
- Auto-scaling based on queue depth (ApproximateNumberOfMessages)
- Circuit breaker on downstream dependencies (database, LLM APIs)

### 4. The Circular Topology is a Latent Bomb

PPS consumes from the call-events topic AND publishes back to the same topic. The PPS doc acknowledges this and calls it the "self-skip guard." But this is fragile:

- If someone adds a new event type that PPS publishes but forgets to add it to `_DISCARDABLE_EVENT_TYPES`, you get an infinite loop.
- If a bug causes the event type to be empty or malformed, the discard check fails and the message gets processed as "unknown" -- potentially triggering another publish.
- At 100x load, even the brief processing time for receiving-and-discarding your own events adds up.

With SNS filter policies (issue #1), this entire problem goes away. PPS should only subscribe to `call_initiated` and `call_completed`, never receiving its own `processing_completed` events.

### 5. No DLQ Consumer or Alerting

Every queue has a DLQ (good), but I see no code anywhere that:
- Monitors DLQ depth
- Processes/replays DLQ messages
- Alerts when messages land in the DLQ

The DLQ is a black hole right now. Messages go in, nobody knows, nobody acts. At scale, you could have hundreds of failed events silently sitting in DLQs without any visibility.

**What's needed**: CloudWatch alarms on `ApproximateNumberOfMessages` for each DLQ, a DLQ consumer or replay mechanism, and Datadog dashboard visibility.

---

## Additional Technical Findings

### Serialization Code Duplication

`CustomJSONEncoder` and `serialize_for_json()` are copy-pasted identically across 3 services (backend, user-services, memory-service). This should be extracted into a shared library. Any bug fix or improvement has to be applied in 3 places.

### Event Schema Drift Risk

Each service defines its own event schemas independently:
- Backend: `CallCompletedEventData` in `backend/event_publishers/events.py`
- User-services: `RecordingSavedEventData`, `CallInitiatedEventData`, `CallEndedEventData`, `CallCompletedEventData` in `app/event_publishers/events.py`
- PPS consumer: `ProcessingCompletedCloudEvent`, `SNSMessage` in `app/event_consumers/schemas.py`
- Memory-service consumer: No typed schema -- raw dict parsing

There is no shared schema registry or contract validation. If the publisher changes the schema, consumers silently break. This is the #1 cause of production incidents in event-driven architectures.

### Timestamp Handling

Some services use `datetime.now()` (naive, local timezone), others use `datetime.utcnow()` (deprecated in Python 3.12+), and memory-service uses `datetime.now(UTC)` (correct). The CloudEvent `time` field is supposed to be RFC 3339 with timezone. Inconsistent timezone handling can cause subtle ordering and deduplication bugs.

### PPS Has Both SNS and SQS Publishers

The PPS `EventPublisherFactory` supports both `aws_sqs` and `aws_sns` providers. The factory pattern and the `event_publishing_config` in the processing config suggest this was migrated from direct SQS publishing to SNS. There may be dead code paths for SQS publishing that should be cleaned up.

---

## Questions to Drive the Discussion

1. **Filter policies -- is there a reason they're not implemented?** The CDK construct supports them, the subscription construct has the `filterPolicy` parameter wired up. Was this a deliberate "ship fast, optimize later" decision, or was it overlooked during the AWS migration? Because this is the single highest-ROI change you can make.

2. **What's the plan for the DLQs?** Right now messages go into DLQs and... nothing happens. At current scale this might be fine, but have you had incidents where events were silently lost? Do you have CloudWatch alarms on DLQ depth? Have you ever needed to replay DLQ messages?

3. **The backend's aioboto3 client-per-call pattern -- was that intentional?** Every SNS publish in the backend creates a new aioboto3 client context. Under burst load (say 50 concurrent calls ending simultaneously), that's 50 new HTTP connections being established in parallel. The user-services pattern of a singleton boto3 client is much more efficient. What's the backend's publish throughput requirement?

4. **Have you considered event schema validation at the consumer side?** Right now, if the PPS changes the `processing_completed` event format, the user-services consumer will silently fail or produce wrong results. A shared schema package (even just Pydantic models in a common repo) with version negotiation would prevent this entire class of bugs. What's the team's appetite for adding a shared events library?

5. **The memory service's `_normalize_event_format` converts everything to "EventGrid" format internally -- is this still needed?** It looks like a vestige of the Azure migration. Every incoming CloudEvent gets converted to `{id, eventType, subject, eventTime, data, dataVersion}` format. This adds complexity and makes the code harder to reason about. If Azure is fully deprecated, this normalization layer could be removed.

6. **What happens when the LLM provider (OpenAI/Anthropic) goes slow during memory creation?** The memory service's `CallerMemoryCreator.execute()` makes LLM calls. If latency spikes from 5 seconds to 30 seconds, your visibility timeout of 120 seconds gives you at most 4 retries before the message hits the DLQ. But during that degradation period, the semaphore (10 concurrent tasks) means your throughput drops to ~0.33 messages/second. If you're processing 100 calls/hour, the queue will grow. Is there any adaptive timeout or circuit breaker on the LLM call path?

7. **The PPS idempotency service uses Redis -- what happens when Redis is down?** I see `_idempotency_graceful_degradation` in the PPS consumer settings. When this is true and Redis fails, the idempotency check is skipped and processing continues. This means you could process duplicates during Redis outages. Is that the intended behavior? What's the downstream impact of duplicate `processing_completed` events reaching user-services and memory-service?
