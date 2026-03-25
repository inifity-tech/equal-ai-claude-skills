# Investigation Patterns Reference

Read this file when symptoms match one of these known failure patterns. Each pattern includes the signature symptoms, the diagnostic shortcut, and common red herrings.

## Table of Contents
1. [Connection Pool Exhaustion](#connection-pool-exhaustion)
2. [Cascading Failure](#cascading-failure)
3. [Thundering Herd](#thundering-herd)
4. [Memory Leak / OOM](#memory-leak--oom)
5. [Deployment-Induced Errors](#deployment-induced-errors)
6. [Database Deadlock / Lock Contention](#database-deadlock--lock-contention)
7. [DNS Resolution Failure](#dns-resolution-failure)
8. [Circuit Breaker Trip](#circuit-breaker-trip)
9. [Rate Limiting / Throttling](#rate-limiting--throttling)
10. [Health Check False Positive](#health-check-false-positive)
11. [Redis Connection Storm](#redis-connection-storm)
12. [Upstream API Degradation](#upstream-api-degradation)

---

## Connection Pool Exhaustion

**Signature symptoms:**
- Sudden spike in response times (not gradual)
- Errors mentioning "connection", "timeout", "pool", or "too many connections"
- Database connections at or near max
- Application CPU/memory look normal (the app is just waiting)

**Diagnostic shortcut:**
Check RDS `DatabaseConnections` metric in CloudWatch. If it's plateaued at a round number (like 50, 100, 200) that matches a pool config, that's your answer.

**Red herrings:**
- High database CPU might be a symptom, not the cause — the connections themselves create overhead
- ALB 5xx errors will spike, but the ALB isn't the problem

**Common causes in Equal AI:**
- Long-running queries holding connections open
- Missing connection release in error paths (check `finally` blocks)
- Sudden traffic spike exceeding pool size
- Connection leak from improperly closed async sessions

---

## Cascading Failure

**Signature symptoms:**
- Multiple services alerting in sequence (not simultaneously)
- Service A errors → Service B latency → Service C timeouts
- The timeline shows a domino effect with 1-5 minute gaps between services

**Diagnostic shortcut:**
Plot the error/latency timelines for all affected services side by side. The one that spiked *first* is usually the origin. Then trace what depends on it.

**Red herrings:**
- The service with the most alerts isn't necessarily the root cause — it might just be the most monitored
- A service might be "alerting" because it can't reach the actual failing service

**Equal AI service dependency chain:**
```
Exotel → ai-backend → Ultravox
                     → Redis
                     → user-services → PostgreSQL
                                     → memory-service → PostgreSQL
                     → post-processing → PostgreSQL → S3
```

---

## Thundering Herd

**Signature symptoms:**
- Service recovers from an incident, then immediately gets worse again
- Spike pattern: down → briefly up → worse than before → slowly recovers
- Load balancer sees massive concurrent request spike
- Often happens right after a deployment or restart

**Diagnostic shortcut:**
Check ALB `RequestCount` and `ActiveConnectionCount`. If there's a sharp spike right after recovery that exceeds normal traffic, clients were retrying with backoff and all hit at once when the service came back.

**Red herrings:**
- Looks like the fix didn't work, but actually the fix exposed a secondary issue

**Mitigation:**
- Check if clients (mobile app, other services) have retry with jitter
- Gradual rollout / canary deployment

---

## Memory Leak / OOM

**Signature symptoms:**
- Gradually increasing memory usage over hours/days, then sudden task kill
- ECS task stopped with reason: "OutOfMemoryError" or "Essential container exited"
- Sawtooth pattern: memory climbs → task killed → new task starts → memory climbs again
- May correlate with specific request patterns or data sizes

**Diagnostic shortcut:**
CloudWatch `MemoryUtilization` for the ECS service. If it's a steady climb (not a spike), it's a leak. Check when the pattern started — was there a deployment?

**Red herrings:**
- High memory might be normal if the service caches data — check the baseline
- Python's memory model doesn't always release memory back to the OS

**Equal AI specific:**
- Audio processing in post-processing service can be memory-intensive
- Large Ultravox responses or long call transcripts held in memory

---

## Deployment-Induced Errors

**Signature symptoms:**
- Errors start within 0-5 minutes of a deployment
- New error messages that didn't exist before
- Only affects the deployed service, not dependencies
- Error rate might be partial (only new tasks show errors, old tasks are fine during rolling deploy)

**Diagnostic shortcut:**
Check ECS deployment events. Cross-reference the deployment time with the error start time. If they match within 5 minutes, read the git log for that service to see what changed.

**Red herrings:**
- Deployment might be coincidental — especially if the errors are on a different service
- Rolling deployments mean both old and new code run simultaneously, which can cause unexpected interactions

---

## Database Deadlock / Lock Contention

**Signature symptoms:**
- Intermittent timeouts on write operations
- Errors mentioning "deadlock detected" or "lock timeout"
- Some requests succeed while others fail (not a total outage)
- Specific endpoints affected, not all database operations

**Diagnostic shortcut:**
Check Datadog APM for the slowest database operations. Look for long-running transactions that might be holding locks. Check if multiple services write to the same tables.

**Red herrings:**
- Read replica lag might look like a database issue but is usually a separate concern
- Connection count spikes during deadlocks but the pool isn't actually exhausted

---

## DNS Resolution Failure

**Signature symptoms:**
- Errors mentioning "Name or service not known", "getaddrinfo failed", "DNS"
- Affects connections to specific hostnames, not IPs
- Often affects multiple services simultaneously (they share DNS)
- Intermittent — some requests succeed between failures

**Diagnostic shortcut:**
Check if the errors consistently mention the same hostname. If it's an RDS or ElastiCache endpoint, check if the service is up independently of DNS.

---

## Circuit Breaker Trip

**Signature symptoms:**
- Service suddenly returns errors for ALL requests to a specific dependency
- Error messages change from varied (timeouts, connection errors) to uniform (circuit open / fast fail)
- Response times drop dramatically (because the circuit breaker short-circuits immediately)
- After a cooldown period, a few requests succeed, then either recover or trip again

**Diagnostic shortcut:**
Look for the transition point — when did errors go from varied to uniform? That's when the circuit breaker tripped. Check what was happening to the dependency just before that point.

---

## Rate Limiting / Throttling

**Signature symptoms:**
- Errors with HTTP 429 status codes
- Errors mentioning "rate limit", "throttled", "too many requests"
- Affects calls to external APIs (Ultravox, OpenAI, Exotel, AWS APIs)
- Often correlates with traffic spikes or batch processing

**Diagnostic shortcut:**
Check Datadog logs for 429 responses. Identify which external API is rate limiting. Check if there was an unusual traffic pattern (batch job, retry storm).

**Equal AI specific:**
- Ultravox API rate limits during high call volume
- OpenAI API limits during post-processing batch runs
- AWS API throttling during infrastructure operations
- Exotel webhooks can burst during concurrent calls

---

## Health Check False Positive

**Signature symptoms:**
- ALB marks targets as unhealthy, but the service appears to be running
- ECS keeps cycling tasks (starts new ones, marks old as unhealthy, drains them)
- Application logs show the health check endpoint responding, but ALB disagrees
- Intermittent — targets flip between healthy and unhealthy

**Diagnostic shortcut:**
Check ALB target group health check configuration — timeout, interval, healthy/unhealthy thresholds. If the health check endpoint does any work (database query, dependency check), a slow response can be mistaken for unhealthy.

**Red herrings:**
- Looks like a deployment issue, but the code is fine — it's the health check timing
- Multiple services flapping might suggest a network issue, but it's actually each service's health check being slow for different reasons

---

## Redis Connection Storm

**Signature symptoms:**
- Spike in Redis connection errors or timeouts
- ElastiCache `CurrConnections` spikes to max
- Multiple services affected simultaneously (they share Redis)
- Session management or real-time features break

**Diagnostic shortcut:**
CloudWatch metrics for the Redis cluster: `CurrConnections`, `EngineCPUUtilization`, `CacheHitRate`. If connections spiked but CPU is normal, it's a connection leak. If CPU spiked, it's a hot key or expensive operation.

**Equal AI specific:**
- ai-backend uses Redis for call session management — connection issues directly affect live calls
- Redis streams for real-time events between services

---

## Upstream API Degradation

**Signature symptoms:**
- Increased latency or error rate on outbound calls to Ultravox, Exotel, OpenAI, SarvamAI
- The Equal AI service itself is healthy (CPU/memory/connections normal)
- Timeout errors dominate the error logs
- Error pattern follows the upstream provider's degradation timeline

**Diagnostic shortcut:**
Check the specific API's status page. Compare the error timestamps with any known upstream incidents. Look at APM traces for outbound HTTP calls — if one specific external host has elevated latency, that's your answer.

**External status pages:**
- Ultravox: check their status page or Slack channel
- OpenAI: status.openai.com
- AWS: health.aws.amazon.com
- Exotel: check with the Exotel team or status page
