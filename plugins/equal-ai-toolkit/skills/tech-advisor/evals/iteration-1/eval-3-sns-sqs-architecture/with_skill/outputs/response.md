# SNS/SQS Event-Driven Architecture Deep Dive

So I've spent a good chunk of time digging through the event-driven architecture across all five services -- backend, user-services, memory-service, post-processing-service, and the CDK infrastructure. I also pulled in current best practices from AWS docs and the community. Here's what I'm seeing.

---

## Doc-Code Discrepancies

Before we get into the substance, I found a few places where the architecture docs and the actual code diverge. These are worth flagging:

1. **User-services LLD-event-system.md references file paths that don't match actual code.** The doc says the publisher hierarchy involves `aws_sns_event_publisher.py` and `event_publisher_manager.py`, but the actual files are `aws_sns.py` and `sns_factory.py`. The abstract base is `base.py`, not `abstract_event_publisher.py`. The doc describes an `EventPublisherManager` that dispatches to multiple publishers, but the actual code uses a singleton factory pattern (`sns_factory.py`) without a multi-provider manager -- unlike the backend service which does have an `EventPublisherManager`. This suggests the doc was written from the backend pattern and copy-adapted without verifying user-services specifics.

2. **Memory-service LLD says "EventGrid-compatible dict format" is a legacy artifact.** This is accurate in the doc, but the actual code in `sqs_consumer.py` line 606-628 still converts CloudEvents to an EventGrid-style dict with `eventType`, `eventTime`, `dataVersion` fields. The doc correctly notes this is a code artifact, not an Azure dependency -- but the code comment says "Convert CloudEvent format to EventGrid" which is misleading for new developers. It's just an internal normalization now.

3. **PPS LLD-event-system.md describes the self-skip guard accurately**, but the actual consumer code (`simple_sqs_consumer.py`) couldn't be fully read due to size. The architecture diagram in the PPS doc shows PPS publishing `processing_completed` back to the *same* SNS topic it consumes from. The CDK events stack (`equalai-events-stack.ts`) confirms this -- there's only one `call-events` SNS topic, and PPS both subscribes to it (via its queue) and publishes back to it. This circular topology is documented but worth highlighting as an architectural risk.

4. **No SNS subscription filter policies are configured anywhere.** The CDK code in `equalai-events-stack.ts` creates subscriptions with `rawMessageDelivery: true` but **no filter policies** on any of the five SQS subscriptions. The `SnsSqsSubscriptionConstruct` supports `filterPolicy` as an optional config, but none of the subscriptions use it. This means every subscriber receives every event type -- PPS gets `processing_completed` events (its own output), memory-service gets `call_initiated` events (irrelevant to it), etc. Each consumer handles this by checking event types in application code and discarding irrelevant ones. The docs don't call this out as an intentional design decision.

---

## Current State: How the Architecture Actually Works

The event-driven backbone of Equal AI is built on a classic SNS fan-out to SQS pattern. There are **two SNS topics** and **six SQS queues** provisioned in the CDK events stack:

**Topic 1: `call-events`** (the main event bus)
- Published to by: backend service (call_ended, call_completed), user-services (recording_saved, call_initiated), and PPS (processing_completed)
- Subscribed by: PPS queue, user-services queue, memory-service queue, evaluations queue, vocab-extraction queue

**Topic 2: `memory-publish`** (reverse flow)
- Published to by: memory-service (caller.data.updated)
- Subscribed by: user-services memory-updates queue

The event flow is a three-stage pipeline:
- **Stage 1**: Backend/user-services publish call lifecycle events (call_initiated, call_completed, recording_saved)
- **Stage 2**: PPS consumes these, runs post-call processing, publishes processing_completed back to the same topic
- **Stage 3**: Memory-service consumes processing_completed, runs LLM-based memory extraction, publishes caller.data.updated to the memory-publish topic, which user-services consumes

All events use **CloudEvents v1.0 spec** -- this is a good, standards-based choice. The message envelope includes `specversion`, `type`, `source`, `id`, `time`, `data`, which gives you a consistent contract across all services.

Infrastructure-wise, the CDK constructs are well-structured. Every SQS queue has a DLQ enabled with `maxReceiveCount: 5`. Queues use `visibilityTimeout: 120s` (3 minutes for vocab extraction). Long polling at 20 seconds. Message retention at 14 days. The DLQ retention is also 14 days. No encryption is enabled on any topic or queue (noted as "can be enabled if needed"). SSM Parameter Store is used to share topic ARNs and queue URLs across services -- a solid pattern that avoids hardcoding.

On the application side, I noticed significant variation in how services implement the publisher/consumer patterns:

| Service | Publisher Pattern | Consumer Pattern | boto3 vs aioboto3 |
|---|---|---|---|
| Backend | `aioboto3` async client per publish (context manager) | N/A (publish-only) | aioboto3 |
| User-services | `boto3` sync client + `asyncio.to_thread()`, singleton | `boto3` sync client + `asyncio.to_thread()`, singleton | boto3 |
| Memory-service | `boto3` sync client + `asyncio.to_thread()`, singleton | `aioboto3` async client (new context manager per poll) | Mixed |
| PPS | `boto3` sync client + `run_in_executor()`, per-config init | Custom `SimpleSQSConsumer` with worker pools | boto3 |

The backend service uses `aioboto3` with async context managers for publishing, while user-services and memory-service use synchronous `boto3` wrapped in `asyncio.to_thread()`. The memory-service consumer uses `aioboto3` but creates a new client context on every poll iteration (line 317: `async with self._session.client("sqs") as sqs_client`), which defeats connection pooling. Meanwhile, user-services consumer creates the client once and reuses it -- the more efficient approach.

---

## External Context: What Best Practices Say

Looking at current AWS recommendations and community patterns (2025-2026):

**SNS Filter Policies**: AWS strongly recommends using [SNS subscription filter policies](https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html) to prevent unnecessary message delivery. Without filters, every subscriber pays the cost of receiving, parsing, and discarding irrelevant events. At your current scale this is manageable, but as volume grows, you're paying for SQS message delivery and consumer CPU cycles to process messages that get immediately discarded.

**DLQ at Two Levels**: AWS recommends DLQs on both the [SQS queue level](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html) AND the [SNS subscription level](https://docs.aws.amazon.com/sns/latest/dg/sns-dead-letter-queues.html). You have SQS DLQs configured for all queues (good), but no SNS subscription-level DLQs. If SNS fails to deliver to an SQS queue (e.g., IAM misconfiguration, queue deleted), those messages are silently lost.

**EventBridge Consideration**: The community consensus in 2025-2026 is that [EventBridge is preferred over SNS for event routing](https://aws.amazon.com/blogs/architecture/best-practices-for-implementing-event-driven-architectures-in-your-organization/) when you need content-based filtering, schema registry, event replay, and archive capabilities. SNS+SQS remains the right choice for high-throughput, simple fan-out. Your use case -- routing different event types to different consumers -- is exactly what EventBridge excels at.

**Idempotency**: The [fan-out pattern demands idempotent consumers](https://nemanjatanaskovic.com/mastering-the-sns-sqs-fan-out-pattern-in-event-driven-systems-2/) since SQS guarantees at-least-once delivery. PPS has Redis-based idempotency (good). Memory-service and user-services do not have explicit idempotency guards -- they rely on the processing logic being naturally idempotent or on the DLQ to eventually remove stuck messages.

---

## My Take

The architecture is fundamentally sound. The SNS fan-out to SQS pattern is the right choice for your event volumes and service count. The CloudEvents standardization, DLQ configuration, and SSM parameter sharing are all good practices.

Where I'd push, though, is on three fronts:

**First, the missing filter policies are the most impactful quick win.** Every SQS queue receives every event type from the `call-events` topic. The PPS queue gets `processing_completed` events that it published itself and has to self-skip. Memory-service gets `call_initiated` and `recording_saved` events it doesn't care about. Each consumer wastes cycles parsing and discarding these. Adding SNS filter policies on `eventType` message attribute would eliminate this at the infrastructure level -- zero code changes needed in the consumers, and you get cost savings plus reduced noise in logs.

**Second, the inconsistent boto3 usage across services creates maintenance burden and subtle bugs.** The memory-service SQS consumer creates a new `aioboto3` client on every poll iteration, while its SNS publisher uses a singleton `boto3` client. User-services does it the other way around. This isn't just aesthetic -- the memory-service consumer pattern means no HTTP connection reuse, which adds latency to every SQS poll. You'd benefit from standardizing on one pattern: either `boto3` + `asyncio.to_thread()` with singleton clients (what user-services does), or `aioboto3` with a long-lived client.

**Third, the single-topic circular topology is a footgun.** PPS publishing `processing_completed` back to the same topic it consumes from works today because of the self-skip guard, but it's inherently fragile. A bug in the skip logic, or a new event type that doesn't get handled, creates an infinite loop. The memory-publish topic already demonstrates the better pattern -- a separate topic for a separate concern. I'd consider splitting `call-events` into two topics: one for input events (call lifecycle) and one for output events (processing results).

---

## Questions to Drive Deeper

1. **The missing filter policies -- was that an intentional "ship fast" decision, or an oversight?** The CDK construct supports `filterPolicy` but none of the five subscriptions use it. Given that the `eventType` message attribute is already set on every published message, adding filter policies would be a CDK-only change. PPS only cares about `call_initiated`, `call_completed`, `recording_saved`. Memory-service only cares about `processing_completed`. Have you calculated how many unnecessary messages each consumer is discarding per day?

2. **The PPS self-skip guard is a critical safety mechanism with no monitoring.** If `_handle_processing_completed_event()` ever fails to delete the message (e.g., SQS delete fails silently), you'd get a reprocessing loop. Is there a Datadog metric tracking how many `processing_completed` events PPS receives and self-skips? And more importantly, have you considered splitting the topics to eliminate the need for this guard entirely?

3. **Memory-service creates a new aioboto3 SQS client on every poll cycle** (line 317 in `sqs_consumer.py`). With 20-second long polling, that's a new TLS handshake every 20 seconds. User-services reuses a singleton boto3 client. Why the difference? Was there a specific issue that led to the per-poll client creation pattern in memory-service?

4. **There's no SNS-level DLQ on any subscription.** If SNS fails to deliver to a queue (permissions change, queue deleted accidentally, etc.), those messages vanish. The SQS DLQ only catches messages that were delivered but couldn't be processed. Have you had any incidents where SNS delivery failures went undetected?

5. **The `DelaySeconds` message attribute is set on every SNS publish call, but SNS doesn't support message delay -- that's an SQS-only feature.** In backend's `aws_sns.py` line 170-173 and user-services' `aws_sns.py` line 183-186, `DelaySeconds` is passed as a message attribute. This doesn't actually delay the message -- it's just metadata that gets ignored. Was this intended as metadata for consumers to use, or is it a leftover from a planned SQS direct-publish path?

6. **User-services has two separate SQS consumers for two different queues** (the main events queue and the memory-updates queue), each with their own singleton factory and polling loop. But they share no common supervisor infrastructure. If the memory-updates consumer silently dies, does anything alert you? The main consumer has health check integration, but I didn't see equivalent monitoring for the `MemoryUpdatesWorker`.

7. **No encryption on any topic or queue.** The CDK code explicitly sets `enableEncryption: false` with a comment "can be enabled if needed." Given that event payloads contain `caller_phone_number`, `caller_name`, `session_id`, and potentially `call_summary` data -- is there a compliance reason this hasn't been enabled? KMS encryption for SQS and SNS is straightforward with CDK and the constructs already support it.

---

These are the threads I'd want to pull on. The biggest bang-for-buck improvements are the filter policies (CDK-only change, immediate cost and noise reduction) and the SNS subscription DLQs (resilience improvement). The topic split and boto3 standardization are medium-term architectural improvements that reduce risk and maintenance burden.

What do you want to dig into first?

---

Sources:
- [AWS Architecture Blog: Best practices for event-driven architectures](https://aws.amazon.com/blogs/architecture/best-practices-for-implementing-event-driven-architectures-in-your-organization/)
- [AWS SNS Subscription Filter Policies](https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html)
- [AWS SNS Dead Letter Queues](https://docs.aws.amazon.com/sns/latest/dg/sns-dead-letter-queues.html)
- [AWS SQS Dead Letter Queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
- [Mastering the SNS+SQS Fan-Out Pattern](https://nemanjatanaskovic.com/mastering-the-sns-sqs-fan-out-pattern-in-event-driven-systems-2/)
- [SNS to SQS Fan-Out: Lessons Learned in Prod](https://awstip.com/the-sns-to-sqs-fan-out-pattern-lessons-learned-in-prod-8936a3158d80)
- [Event Buses: SNS/SQS vs EventBridge - 2025 Best Practices](https://www.sachith.co.uk/event-buses-sns-sqs-vs-eventbridge-vs-event-grid-best-practices-in-2025-practical-guide-feb-7-2026/)
- [Event-Driven Design: Choosing Between SNS, SQS, and EventBridge](https://dev.to/aws-builders/event-driven-design-choosing-between-sns-sqs-and-eventbridge-i82)
- [Designing Durable Serverless Apps with DLQs](https://aws.amazon.com/blogs/compute/designing-durable-serverless-apps-with-dlqs-for-amazon-sns-amazon-sqs-aws-lambda/)
