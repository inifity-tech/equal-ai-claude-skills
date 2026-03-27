# SNS/SQS Event-Driven Architecture Deep-Dive

So I've spent a good chunk of time digging through the entire event-driven architecture across all your services -- the CDK infrastructure, the publishers, the consumers, the settings, the lifespan handlers. Here's what I'm seeing, and some of it is genuinely strong work while other parts have meaningful gaps that will bite you at scale.

---

## Current State: How the Architecture Actually Works

### The Topology

You have a classic **SNS fan-out pattern** with two independent event flows:

**Flow 1: Call Events (primary)**
```
Backend Service (user-services) --SNS publish--> Call Events Topic
                                                    |
                            +-----------------------+-----------------------+-------------------+-------------------+
                            |                       |                       |                   |                   |
                            v                       v                       v                   v                   v
                    PPS Queue               User-Services Queue      Memory Queue       Evaluations Queue    Vocab Extraction Queue
                    (SQS)                   (SQS)                    (SQS)              (SQS)                (SQS)
                    |                       |                        |                   |                   |
                    v                       v                        v                   v                   v
                SimpleSQSConsumer      SQS Consumer             SQSEventConsumer    Evaluations Consumer   Lambda
                (PPS)                  (user-services)           (memory-service)
```

**Flow 2: Memory Publish Events (reverse flow)**
```
Memory Service --SNS publish--> Memory Publish Topic
                                      |
                                      v
                              Memory-Publish-User-Services Queue (SQS)
                                      |
                                      v
                              MemoryUpdatesWorker (user-services)
```

This is defined in `/Users/swapnilpakhare/equal-ai/myequal-ai-cdk/equalai/stacks/common/equalai-events-stack.ts`, and the topology is sound. Fan-out via SNS to per-service SQS queues is the textbook approach for decoupled microservices. Each service gets its own queue, processes at its own pace, and failures are isolated.

### Publishers

Three services publish events:

1. **user-services** (`/Users/swapnilpakhare/equal-ai/myequal-ai-user-services/app/event_publishers/aws_sns.py`): Publishes `call_initiated`, `call_completed`, and `recording_saved` events to the Call Events topic. Uses `boto3` with `asyncio.to_thread()` for non-blocking I/O. Uses `tenacity` for retries with exponential backoff.

2. **post-processing-service** (`/Users/swapnilpakhare/equal-ai/myequal-post-processing-service/app/services/event_publishers/aws_sns.py`): Publishes `processing_completed` events to the same Call Events topic. Similar pattern.

3. **memory-service** (`/Users/swapnilpakhare/equal-ai/memory-service/app/event_publishers/aws_sns.py`): Publishes `caller_data_updated` events to the Memory Publish topic. Fire-and-forget from background tasks tracked via a module-level `_background_tasks` set.

### Consumers

Each service has its own consumer implementation:

- **user-services** (`/Users/swapnilpakhare/equal-ai/myequal-ai-user-services/app/main.py`, lines 134-533): ~400 lines of inline consumer logic in the lifespan handler. Uses semaphore-controlled concurrent processing, supervisor pattern, readiness gate.

- **memory-service** (`/Users/swapnilpakhare/equal-ai/memory-service/app/event_consumers/sqs_consumer.py`): Clean `SQSEventConsumer` class extending `BaseEventConsumer`. Uses `aioboto3` for async operations, semaphore concurrency control, health checks.

- **post-processing-service** (`/Users/swapnilpakhare/equal-ai/myequal-post-processing-service/app/consumers/simple_sqs_consumer.py`): `SimpleSQSConsumer` with Redis-based idempotency, signal handling, exponential backoff with jitter, per-workflow-type timeouts.

---

## What's Strong

### 1. Infrastructure Constructs (CDK)

The CDK constructs are well-structured. The `SqsQueueConstruct` in `/Users/swapnilpakhare/equal-ai/myequal-ai-cdk/commons-lib/src/constructs/sqs-queue-construct.ts` creates DLQs by default (line 45: `if (config.dlqEnabled !== false)`), uses long polling (line 70: `receiveMessageWaitTime: cdk.Duration.seconds(20)`), and has proper retention policies. The construct supports FIFO queues, encryption, and configurable DLQ receive counts. Each queue gets proper tags for cost tracking. SSM parameters store queue URLs and topic ARNs for service consumption.

### 2. Fire-and-Forget Publishing Pattern

All three publisher services correctly use fire-and-forget semantics. The user-services publisher in `/Users/swapnilpakhare/equal-ai/myequal-ai-user-services/app/event_publishers/__init__.py` wraps publish calls in `asyncio.create_task()` (line 143) so the main request path is never blocked by event publishing. The memory service tracks background tasks in a set and drains them on shutdown (lines 354-370 of `main.py`). This is exactly right.

### 3. Supervisor Pattern

All three services implement a supervisor that monitors the SQS consumer task, detects crashes, and restarts with a configurable delay. This is a production-critical pattern. The user-services implementation uses linear backoff (delay * restart_count), memory-service does the same, and PPS has a simpler 10-second fixed delay. The configurable `max_restarts` limit (default 5) prevents infinite restart loops.

### 4. Readiness Gate (user-services)

The `app_ready = asyncio.Event()` in user-services' lifespan is smart. SQS consumers wait for this event before processing messages, which prevents OOM kills from processing a backlog during startup while the app is still initializing. This is production wisdom.

### 5. Connection Pooling Configuration

The user-services settings show explicit `AWSSQSPoolSettings` and `AWSSNSPoolSettings` with configurable pool sizes, timeouts, and adaptive retry modes. This level of control is good for tuning under load.

### 6. CloudEvents Format

All publishers emit events in CloudEvents 1.0 format with `specversion`, `type`, `source`, `id`, `time`, and `data` fields. Consumers handle both CloudEvents and legacy formats. Standardizing on CloudEvents is the right long-term choice.

### 7. Idempotency in PPS

The PPS consumer has Redis-based idempotency with lock acquisition, graceful degradation (processes anyway if Redis is down), and lock release on failure. The user-services consumer has application-level idempotency checks (`_is_already_processed` checks a flag in the DB). These protect against at-least-once delivery issues.

---

## What Could Be Better -- And This Is Where It Gets Interesting

### Critical Finding 1: No SNS Message Filtering -- Every Queue Gets Every Message

This is the single biggest architectural gap. Looking at the CDK events stack (`/Users/swapnilpakhare/equal-ai/myequal-ai-cdk/equalai/stacks/common/equalai-events-stack.ts`), every subscription is created with `rawMessageDelivery: true` and **no filter policy**:

```typescript
// Lines 118-123: PPS Subscription - NO filter policy
const postProcessingSubscription = new SnsSqsSubscriptionConstruct(this, "PostProcessingSubscription");
postProcessingSubscription.build(this.props, {
    topic: topicOutput.topic,
    queue: postProcessingQueueOutput.queue,
    rawMessageDelivery: true
    // NO filterPolicy!
});
```

This means:
- **Every event goes to every queue**. When user-services publishes a `call_initiated` event, it lands in all 5 queues -- including the user-services queue that doesn't care about `call_initiated`.
- **Consumers waste cycles parsing and discarding irrelevant messages.** In user-services' consumer (line 285), unknown event types are acknowledged and discarded. The memory-service consumer (line 671-676) deletes unknown event types immediately. PPS maps `call_initiated` and `recording_saved` to workflows and ignores others.
- **You're paying for 5x the SQS message deliveries you need.** SNS delivers to all subscribers, and each delivery is a billable SQS write.

The CDK construct **supports** filter policies (see `sns-sqs-subscription-construct.ts` line 13: `filterPolicy?: { [attribute: string]: sns.SubscriptionFilter }`), but they're never used.

**Recommendation:** Add filter policies based on the `eventType` message attribute that publishers already include. For example:

- PPS queue: filter on `eventType` IN `["call_initiated", "recording_saved"]`
- User-services queue: filter on `eventType` IN `["processing_completed", "call_summary_updated"]`
- Memory-service queue: filter on `eventType` = `"processing_completed"`
- Evaluations queue: filter on `eventType` = `"processing_completed"`

This is free at the SNS level and eliminates unnecessary SQS writes, receive operations, and consumer processing.

### Critical Finding 2: Massive Consumer Code in Lifespan Handler (user-services)

The user-services `main.py` at `/Users/swapnilpakhare/equal-ai/myequal-ai-user-services/app/main.py` has approximately **400 lines of inline SQS consumer logic inside the `lifespan()` function** (lines 134-533). This includes the message processing function, the polling loop, and the supervisor -- all as nested closures.

Compare this with memory-service, which has a clean `SQSEventConsumer` class in a dedicated module, or PPS which has `SimpleSQSConsumer` as a standalone class. The user-services approach:

- Makes testing extremely difficult (can't unit test the consumer without starting the full app)
- Makes the lifespan handler over 800 lines long
- Mixes infrastructure lifecycle concerns with business logic
- Uses `nonlocal` and closure state that's hard to reason about

**Recommendation:** Extract the consumer into a class like `CallEventsWorker` (similar to the existing `MemoryUpdatesWorker` at `/Users/swapnilpakhare/equal-ai/myequal-ai-user-services/app/event_consumers/memory_updates_worker.py`, which is already well-structured).

### Critical Finding 3: Inconsistent Consumer Implementations Across Services

Each service has implemented its own SQS consumer from scratch. Here's the comparison:

| Feature | user-services (main) | user-services (memory worker) | memory-service | PPS |
|---|---|---|---|---|
| Client | Custom SQS consumer (factory) | MemoryUpdatesWorker | aioboto3 | aioboto3 |
| Concurrency | Semaphore | Semaphore | Semaphore | Semaphore |
| Tracing | DD spans per message | DD spans | DD spans | DD spans |
| Idempotency | DB-level check | N/A | N/A | Redis locks |
| Backoff on error | `sleep(5)` | Configurable | `sleep(min(10, interval))` | Exponential + jitter |
| Message format | SNS+CloudEvents | Direct CloudEvents | SNS+CloudEvents | SNS+CloudEvents |
| Visibility extension | None | None | None | None |
| Health check | None (for call events) | None | Detailed | Detailed |
| Signal handling | No | No | No | Yes (SIGTERM/SIGINT) |

This duplication means:
- Fixes in one consumer don't propagate to others
- Inconsistent error handling behavior
- Inconsistent metrics naming
- Maintenance burden of 4 different consumer implementations

**Recommendation:** Create a shared `BaseWorker` pattern (or extend the existing `BaseEventConsumer` in memory-service) and use it across services. The PPS `SimpleSQSConsumer` is the most robust implementation with idempotency, jitter, and signal handling.

### Critical Finding 4: No Visibility Timeout Extension (Heartbeat Pattern)

None of the consumers implement the SQS heartbeat pattern -- extending the visibility timeout while processing is ongoing. The current setup:

- **PPS**: visibility_timeout=120s, message_processing_timeout=60s (usually safe)
- **Memory-service**: visibility_timeout=300s, message_processing_timeout=90s (safe but 300s is long)
- **User-services**: visibility_timeout=120s, message_processing_timeout=30s (safe)

But the memory-service processes events that involve LLM calls (Gemini) which can take 15+ seconds each, and the PPS processes full audio transcription workflows. If processing exceeds the visibility timeout, SQS will make the message visible again, and another consumer instance (or the same one on the next poll) will pick it up -- leading to duplicate processing.

AWS best practices explicitly recommend: "If you don't know how long it takes to process a message, create a heartbeat for your consumer process: specify the initial visibility timeout (for example, 2 minutes) and then -- as long as your consumer still works on the message -- keep extending the visibility timeout by 2 minutes every minute." (Source: [AWS SQS Best Practices](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/best-practices-processing-messages-timely-manner.html))

**Recommendation:** Implement a visibility extension task that periodically extends the timeout while processing is in progress. This is especially critical for the PPS `recording_saved` workflow where audio downloading + transcription + summarization can take minutes.

### Critical Finding 5: DLQ Monitoring and Redrive Not Addressed

The CDK creates DLQs for every queue (good), with `dlqMaxReceiveCount: 5` for most queues and 3 for the memory publish queue. But:

1. **No DLQ alarms.** There are no CloudWatch alarms on DLQ depth (`ApproximateNumberOfMessagesVisible`). Messages could silently accumulate in DLQs without anyone knowing. This is an AWS Well-Architected anti-pattern.

2. **No redrive policy implementation.** There's no mechanism to replay messages from DLQs after the root cause is fixed. AWS supports DLQ redrive (moving messages back to the source queue), but this needs to be either set up via console or automated.

3. **No DLQ consumer.** Nobody processes DLQ messages for alerting, auditing, or analysis.

**Recommendation:** At minimum, add CloudWatch alarms on every DLQ with threshold >= 1. Better: add a Lambda that fires on DLQ messages and sends Slack/PagerDuty alerts. Best: implement a redrive mechanism.

### Critical Finding 6: Backfill Consumer Uses Synchronous boto3

The backfill consumer at `/Users/swapnilpakhare/equal-ai/memory-service/app/event_consumers/backfill_consumer.py` uses synchronous `boto3.client("sqs")` (line 52) and calls `self.sqs_client.receive_message()` synchronously (line 80) inside an `async` method. This blocks the event loop during SQS API calls. The main live consumer in the same service correctly uses `aioboto3`, but the backfill consumer doesn't.

For backfill with single-message processing this might be acceptable if it runs standalone, but if it ever shares an event loop with the main service, it'll cause latency spikes.

### Critical Finding 7: No Encryption at Rest

Looking at the events stack:

```typescript
enableEncryption: false // Repeated for every queue and topic
```

None of the queues or topics use KMS encryption. The CDK constructs support it (`enableEncryption` parameter, KMS key creation), but it's explicitly disabled. Depending on your compliance requirements, this may be acceptable for development/test but should be evaluated for production -- especially since messages may contain call summaries, phone numbers, and other PII.

### Critical Finding 8: Thread Pool Sizing Could Cause Starvation

Both user-services and memory-service configure the default thread pool with `min(64, (os.cpu_count() or 4) * 16)` workers. On a 4-vCPU ECS task, that's 64 threads. The SNS publisher uses `asyncio.to_thread()` which shares this pool. Under high load with many concurrent SQS consumers + SNS publishes + S3 operations all using `to_thread()`, you could exhaust the thread pool.

Consider: with 10 concurrent SQS message processing tasks in memory-service, each doing LLM calls and then SNS publishing, plus 40+ database connections (pool_size=40), the 64-thread pool could become a bottleneck. The `to_thread()` calls would queue up, adding latency to everything.

**Recommendation:** Consider dedicated thread pools for different I/O categories (SQS operations, SNS publishing, S3 operations) rather than sharing the default executor. Or use native async clients (aioboto3) consistently instead of `boto3` + `to_thread()`.

### Critical Finding 9: Raw Message Delivery Inconsistency

All subscriptions use `rawMessageDelivery: true`, which means SQS messages contain the raw SNS message body without the SNS envelope. But consumers still handle both formats:

- User-services consumer (line 238): `if "Message" in body_data and "Type" in body_data:` -- handles SNS envelope format
- Memory-service consumer (line 560): `if "Type" in body_data and body_data["Type"] == "Notification":` -- handles SNS envelope format

With raw message delivery enabled, these SNS envelope branches should never execute in production. They're dead code that adds complexity. If you ever accidentally flip `rawMessageDelivery` to `false`, the behavior would silently change.

**Recommendation:** If you're committed to raw message delivery (which is the right choice for performance), remove the SNS envelope handling code or add logging when it's hit to detect misconfiguration.

### Finding 10: Event ID Generation Is Not Idempotent-Friendly

The user-services SNS publisher generates event IDs using timestamps:

```python
"id": f"{event_type}-{datetime.now().timestamp()}"
```

This means every publish attempt generates a new event ID, even for the same logical event. If the same call event is published twice (e.g., due to a retry at the application level), the IDs will differ, making downstream idempotency checks based on event ID ineffective.

**Recommendation:** Use a deterministic ID derived from the event content (e.g., `f"{event_type}-{session_id}"`) so that duplicate publishes of the same event produce the same ID.

---

## External Context: What Best Practices Say

The current architecture aligns well with the core SNS fan-out pattern as recommended by AWS. Here's what the latest guidance says about areas you could improve:

1. **"Filter at the edge, batch in the middle, and poll efficiently at the end."** You're polling efficiently (long polling is configured), but you're not filtering at the edge (no SNS filter policies). Source: [Understanding SNS & SQS costs, patterns, and practical optimizations](https://medium.com/@deleemarf/understanding-sns-sqs-costs-patterns-and-practical-optimizations-for-devs-devops-b111e5a89f7b)

2. **SNS message filtering is free and eliminates unnecessary SQS deliveries.** "With payload-based message filtering, you have a simple, no-code option to further prevent unwanted data from being delivered to subscriber systems." Source: [AWS SNS message filtering](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html)

3. **DLQ monitoring is mandatory.** "ApproximateNumberOfMessagesVisible captures all messages currently available in the DLQ. Setting an alarm to trigger on any visible messages (threshold >= 1) is a common approach since messages in a DLQ represent failures that need investigation." Source: [AWS DLQ best practices](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)

4. **Visibility timeout heartbeat.** "If you don't know how long it takes to process a message, create a heartbeat for your consumer process." Source: [Processing messages in a timely manner](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/best-practices-processing-messages-timely-manner.html)

5. **Don't set maxReceiveCount to 1.** A single transient failure would send messages to the DLQ. Your current setting of 3-5 is appropriate.

---

## My Take: Priority Ranking

If I were advising on what to tackle first:

1. **SNS filter policies** (high impact, low effort) -- This is a CDK-only change. No application code changes needed. Immediately reduces unnecessary message processing and SQS costs across 5 queues. Implement this week.

2. **DLQ monitoring and alerting** (high impact, medium effort) -- You have DLQs but you're flying blind. Add CloudWatch alarms. This is a CDK change + whatever alerting integration you use.

3. **Extract user-services inline consumer** (medium impact, medium effort) -- The `MemoryUpdatesWorker` pattern already exists in user-services and is well-structured. Clone that pattern for the call events consumer.

4. **Visibility timeout heartbeat for PPS** (high impact for reliability, medium effort) -- PPS processes long-running workflows. A heartbeat prevents duplicate processing during slow transcriptions.

5. **Consistent consumer pattern** (medium impact, high effort, long-term) -- Converge on a shared consumer base class. This is a refactor that pays off over time but isn't urgent.

6. **Event ID determinism** (low effort, prevents subtle bugs) -- Quick fix to use session_id-based event IDs.

---

## Questions to Drive This Deeper

1. **Have you seen duplicate processing in production?** Without visibility timeout extension and with at-least-once delivery, I'd expect occasional duplicates -- especially for slow PPS workflows. The `updated_call_summary_via_postprocessing` idempotency flag in user-services suggests this has been a problem. What's the actual duplicate rate?

2. **What's your DLQ depth right now?** I didn't find any monitoring on DLQs. There could be messages sitting in DLQs right now that represent lost call summaries or unprocessed memories. Have you checked recently?

3. **The `delay_seconds` on SNS publish (default 3s) -- what's the purpose?** The user-services publisher sets `DelaySeconds` as a message attribute. But SNS message attributes don't natively delay SQS delivery. This looks like it's intended to set SQS delivery delay, but that's a queue-level setting, not a per-message attribute via SNS. Is this actually doing what you intend?

4. **Why does the memory-service have visibility_timeout=300s (5 minutes) when the CDK sets it to 120s?** The CDK creates queues with `visibilityTimeout: cdk.Duration.seconds(120)`, but the memory-service settings have `visibility_timeout: int = Field(default=300)`. The consumer passes this to `receive_message()` which overrides the queue default. Is 300s intentional? That's a long time to hold a message invisible.

5. **The backfill consumer uses synchronous boto3 in an async context -- is that intentional?** It processes one message at a time with 30-minute visibility, so the blocking nature might be acceptable. But if you ever want to parallelize backfill, this will be a problem.

6. **What happens if the Call Events topic has a temporary outage?** The publishers use fire-and-forget (`asyncio.create_task`). If SNS is temporarily unreachable, the background task logs a warning and the event is lost. There's no local queue or retry-from-source mechanism. For critical events like `call_initiated`, is this acceptable? Or should there be a fallback (e.g., write to a local table and retry)?

7. **Have you considered the thundering herd scenario on PPS?** When the supervisor restarts a crashed PPS consumer, it re-reads from the queue immediately. If the queue has a backlog of messages from the outage period, all messages become visible simultaneously and the consumer tries to process them all at once. With `max_concurrent_tasks=10` and each task doing heavy audio processing, this could OOM the container. The user-services readiness gate pattern (`app_ready` event) could be valuable here too.

Sources:
- [Mastering the SNS + SQS Fan-Out Pattern](https://nemanjatanaskovic.com/mastering-the-sns-sqs-fan-out-pattern-in-event-driven-systems-2/)
- [AWS SNS message filtering docs](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html)
- [AWS SQS visibility timeout docs](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)
- [AWS SQS best practices](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-best-practices.html)
- [AWS SQS DLQ docs](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
- [Processing messages in a timely manner](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/best-practices-processing-messages-timely-manner.html)
- [AWS Well-Architected SQS best practices](https://aws.amazon.com/blogs/compute/implementing-aws-well-architected-best-practices-for-amazon-sqs-part-2/)
- [SNS SQS costs and optimizations](https://medium.com/@deleemarf/understanding-sns-sqs-costs-patterns-and-practical-optimizations-for-devs-devops-b111e5a89f7b)
- [SNS+SQS fan-out pattern lessons](https://awstip.com/the-sns-to-sqs-fan-out-pattern-lessons-learned-in-prod-8936a3158d80)
