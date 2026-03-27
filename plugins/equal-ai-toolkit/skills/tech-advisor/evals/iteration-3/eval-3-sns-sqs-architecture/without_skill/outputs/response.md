# SNS/SQS Event-Driven Architecture Review

## Architecture Overview

After exploring the codebase across all five services, here is the event flow topology:

```
Backend Service (myequal-ai-backend)
  |
  | publishes: call_completed
  v
SNS Topic: call-events
  |
  |-- fan-out --> SQS: post-processing-service-events  --> Post-Processing Service
  |-- fan-out --> SQS: user-services-events             --> User Services
  |-- fan-out --> SQS: memory-events                    --> Memory Service
  |-- fan-out --> SQS: evaluations-service-events       --> Evaluations Service
  |-- fan-out --> SQS: vocab-extraction-events           --> Vocab Extraction Lambda
  |
Post-Processing Service
  | publishes: processing_completed (to same SNS topic)
  v
SNS Topic: call-events (same topic, downstream services pick up)

Memory Service
  | publishes: caller.data.updated
  v
SNS Topic: memory-publish-events
  |
  |-- fan-out --> SQS: memory-publish-events-user-services --> User Services
```

All subscriptions use **raw message delivery** (good) and all queues have **DLQs enabled** with maxReceiveCount of 3-5.

---

## What You Are Doing Well

### 1. CloudEvents Specification Compliance
All services wrap events in CloudEvents v1.0 format with `specversion`, `type`, `source`, `id`, `time`, and `data` fields. This is a strong foundation for interoperability and gives you a standardized envelope.

### 2. Fan-Out Pattern with SNS
Using a single SNS topic with multiple SQS subscribers is textbook AWS event-driven architecture. This cleanly decouples producers from consumers and lets you add new consumers without modifying publishers.

### 3. Dead Letter Queues on Every Queue
Every SQS queue in the CDK stack has DLQ enabled (with 3-5 max receive count). This prevents poison messages from blocking processing.

### 4. Raw Message Delivery
All SNS-to-SQS subscriptions default to `rawMessageDelivery: true`. This avoids the SNS wrapper overhead and simplifies consumer parsing.

### 5. Infrastructure-as-Code with SSM Parameters
The CDK stack stores topic ARNs and queue URLs in SSM Parameter Store, which cleanly separates infrastructure from application config.

### 6. Connection Pooling and Singleton Patterns
The user-services and memory-service both use singleton factories with thread-safe initialization for their SQS consumers and SNS publishers. The user-services SNS publisher uses `boto3` with `botocore.Config` for connection pooling, while the memory service follows the same pattern.

### 7. Idempotency Service (Post-Processing)
The post-processing service has an `EventIdempotencyService` using Redis SET NX for exactly-once processing semantics. This is a best practice that protects against SQS at-least-once delivery duplicates.

### 8. Worker Pool with Backpressure
The post-processing service's `WorkerPool` with semaphore-based concurrency control and backpressure detection (`is_at_capacity`) is well-designed for handling bursty workloads.

---

## Issues and Improvement Opportunities

### Critical: No SNS Message Filtering

**The biggest gap.** The CDK events stack creates subscriptions with **no filter policies** whatsoever. Every subscriber receives every event type published to the `call-events` topic.

Evidence from `equalai-events-stack.ts`:
```typescript
// All subscriptions look like this -- no filterPolicy:
postProcessingSubscription.build(this.props, {
    topic: topicOutput.topic,
    queue: postProcessingQueueOutput.queue,
    rawMessageDelivery: true
    // NO filterPolicy
});
```

This means:
- **User services, memory service, and evaluations service** all receive `call_completed` and `call_initiated` events they do not care about (they only want `processing_completed`).
- The **memory service SQS consumer** explicitly discards events with `conversation_type` and handles unknown event types by deleting them -- this is wasted compute and SQS API calls.
- The post-processing service's consumer has a `_DISCARDABLE_EVENT_TYPES` frozenset that includes `processing_completed` -- meaning it receives its own output events and discards them.

**Recommendation:** Add SNS subscription filter policies based on the `eventType` message attribute that you already set on every published message:

```typescript
// Example: Memory service only needs processing_completed
memoryServiceSubscription.build(this.props, {
    topic: topicOutput.topic,
    queue: memoryServiceQueueOutput.queue,
    rawMessageDelivery: true,
    filterPolicy: {
        eventType: sns.SubscriptionFilter.stringFilter({
            allowlist: ['processing_completed']
        })
    }
});

// Post-processing only needs call_initiated, call_completed, recording_saved
postProcessingSubscription.build(this.props, {
    topic: topicOutput.topic,
    queue: postProcessingQueueOutput.queue,
    rawMessageDelivery: true,
    filterPolicy: {
        eventType: sns.SubscriptionFilter.stringFilter({
            allowlist: ['call_initiated', 'call_completed', 'recording_saved']
        })
    }
});
```

This reduces unnecessary SQS message volume, saves cost, and eliminates wasted processing.

---

### High: Massive Code Duplication Across Services

The `CustomJSONEncoder`, `serialize_for_json`, and CloudEvent creation logic are copy-pasted across **four separate services**:

| File | Service |
|------|---------|
| `myequal-ai-backend/backend/event_publishers/aws_sns.py` | Backend |
| `myequal-ai-user-services/app/event_publishers/aws_sns.py` | User Services |
| `memory-service/app/event_publishers/aws_sns.py` | Memory Service |
| `myequal-post-processing-service/app/services/event_publishers/aws_sns.py` | Post-Processing |

Each has a slightly different variant:
- Backend uses `aioboto3` (async native)
- User-services and memory-service use `boto3` with `asyncio.to_thread()`
- Post-processing uses `boto3` with `loop.run_in_executor()`
- Backend's publisher inherits from `AbstractEventPublisher`, user-services wraps it in `SNSEventPublisherWrapper`, PPS uses a completely different `EventPublisherInterface`

**Recommendation:** Extract a shared `equal-ai-events` Python package (or at minimum a shared module) with:
- Common CloudEvent serialization
- Standardized SNS publisher base class
- Common SQS consumer base class
- Shared event type constants and schemas

---

### High: Inconsistent Async I/O Patterns

Three different approaches to non-blocking AWS calls:

1. **Backend**: `aioboto3` (truly async)
2. **User-services / Memory-service SNS publishers**: `boto3` + `asyncio.to_thread()`
3. **Post-processing / Memory-service SQS consumer**: `aioboto3` context managers (creating a new client per operation)

The memory service SQS consumer creates a **new SQS client per `receive_message` and `delete_message` call** via `async with self._session.client("sqs")`. This defeats connection pooling:

```python
# memory-service/app/event_consumers/sqs_consumer.py line 317
async with self._session.client("sqs") as sqs_client:
    response = await sqs_client.receive_message(...)
```

Compare this to user-services' SQS consumer, which creates one `boto3` client in `__init__` and reuses it.

**Recommendation:** Standardize on `boto3` + `asyncio.to_thread()` with a single client per service (as user-services does), or use `aioboto3` but keep the client alive across operations rather than creating/destroying it per call. The `asyncio.to_thread()` pattern is simpler and avoids the `aioboto3` client lifecycle complexity.

---

### Medium: No Encryption on Any SNS Topic or SQS Queue

Every topic and queue has `enableEncryption: false`:

```typescript
// equalai-events-stack.ts
topicConstruct.build(this.props, {
    topicName: `...`,
    enableEncryption: false // <-- every resource
});
```

While you are within a VPC and may consider the risk acceptable, AWS best practices recommend server-side encryption (SSE) for both SNS and SQS, especially for data containing user/caller information. At minimum, use `SQS_MANAGED` (SSE-SQS) which is free and requires no KMS key management.

**Recommendation:** Enable `sqs.QueueEncryption.SQS_MANAGED` encryption on all queues and SNS topic encryption with the AWS-managed key.

---

### Medium: Inconsistent Event ID Generation

Event IDs are generated differently across services:
- **Backend**: Uses `event.event_id` from `BaseEvent` (presumably a UUID)
- **User-services**: `f"{event_type}-{datetime.now().timestamp()}"` -- timestamp-based, not globally unique
- **Memory-service**: `f"{event_type}-{uuid.uuid4()}"` -- UUID-based, good
- **Post-processing**: Uses the `CloudEvent.id` field

The user-services pattern (`event_type-timestamp`) could produce collisions under concurrent load (two events of the same type within the same timestamp resolution).

**Recommendation:** Use UUIDs (v4 or v7) consistently for event IDs across all services.

---

### Medium: Visibility Timeout May Be Too Low

All queues use 120-second visibility timeout, but the memory service processes events that involve LLM calls taking 10-15+ seconds, and the post-processing service runs multi-step workflows. If processing takes longer than 120 seconds, SQS will make the message visible again, causing duplicate processing.

The post-processing service's `SimpleSQSConsumer` does not extend the visibility timeout during long-running processing. Only the vocab extraction queue has a higher timeout (180s).

**Recommendation:**
- Implement visibility timeout extension (heartbeat) for long-running consumers
- Or increase visibility timeout to match the `message_processing_timeout` setting (90s in memory service, but workflows in PPS could be longer)
- The rule of thumb: visibility timeout should be at least 6x the expected processing time

---

### Medium: Fire-and-Forget Publishing Without Delivery Confirmation

The backend service's `EventPublisherManager.publish_event_fire_and_forget` uses `asyncio.create_task()` with no tracking:

```python
asyncio.create_task(self._publish_event_background(event, **kwargs))
```

If the application shuts down while background tasks are pending, events will be silently lost. The user-services has the same pattern.

**Recommendation:**
- Track background publish tasks and drain them on shutdown (similar to how the PPS worker pool does it)
- Consider adding a publish confirmation callback or at minimum logging unfinished tasks during shutdown
- Alternatively, use a transactional outbox pattern for critical events

---

### Low: Missing DLQ Monitoring and Alerting

DLQs are created for every queue but there is no evidence of:
- CloudWatch alarms on DLQ message count
- DLQ processing/replay mechanisms
- Alerting when messages land in DLQs

**Recommendation:** Add CloudWatch alarms for `ApproximateNumberOfMessagesVisible > 0` on all DLQs, with SNS notifications to an ops channel.

---

### Low: Hardcoded Source Strings

Event source identifiers are hardcoded strings scattered across services:
- `"myequal-ai/user-services"` in user-services SNS publisher
- `"myequal-ai/memory-service"` in memory service
- Backend uses `event.source` from the event object

**Recommendation:** Define source identifiers as constants (ideally in the shared events package).

---

### Low: `loop.run_in_executor` vs `asyncio.to_thread`

The post-processing service's SNS and SQS publishers use the deprecated pattern:
```python
loop = asyncio.get_event_loop()
return await loop.run_in_executor(None, lambda: ...)
```

While `asyncio.to_thread()` (used by user-services and memory-service) is the modern equivalent and preferred since Python 3.9.

**Recommendation:** Migrate PPS publishers to use `asyncio.to_thread()`.

---

## Summary of Priorities

| Priority | Issue | Impact |
|----------|-------|--------|
| Critical | No SNS filter policies | Wasted compute, unnecessary SQS costs, all services process irrelevant events |
| High | Code duplication across 4 services | Maintenance burden, drift risk, inconsistent behavior |
| High | Inconsistent async I/O (memory svc creates new client per call) | Performance degradation, connection pool waste |
| Medium | No encryption on topics/queues | Security compliance gap |
| Medium | Inconsistent event ID generation | Potential ID collisions in user-services |
| Medium | Visibility timeout too low for long workflows | Duplicate processing risk |
| Medium | Fire-and-forget without shutdown drain | Silent event loss on deployment |
| Low | No DLQ monitoring/alerting | Undetected message failures |
| Low | Hardcoded source strings | Maintenance overhead |
| Low | Deprecated `run_in_executor` in PPS | Code modernization |

The architecture is fundamentally sound -- the SNS fan-out with per-service SQS queues, CloudEvents envelope, DLQs, and idempotency service are all good patterns. The highest-value improvement would be adding SNS filter policies, which is a straightforward CDK change that immediately reduces waste and cost. The second priority would be extracting shared event infrastructure code to eliminate the four-way duplication.
