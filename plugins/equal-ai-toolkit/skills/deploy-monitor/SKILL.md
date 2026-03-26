---
name: deploy-monitor
description: Post-deployment production monitoring — checks ALB, ECS, logs, RDS, Redis, SQS/SNS, Datadog monitors, and Slack alerts on a recurring interval. Use after CDK deploys, ECS rolling updates, or any production change.
disable-model-invocation: false
---

# Post-Deployment Production Monitor

Monitors production health during and after deployments by checking 9 infrastructure categories on a recurring interval. Captures a baseline snapshot first, then reports only deviations.

## Parameters

`$ARGUMENTS` can include:
- `--duration <N>m` — monitoring duration (default: `10m`, max: `30m`)
- `--interval <N>m` — check interval (default: `2m`, min: `1m`)
- `--env <environment>` — environment to monitor (default: `prod`)
- `--services <list>` — comma-separated services to focus on (default: all — `ai-backend,user-services,memory-service,api-gateway`)
- `--skip <categories>` — comma-separated categories to skip (e.g., `--skip redis,sns`)

Example: `/deploy-monitor`, `/deploy-monitor --duration 20m --interval 3m`, `/deploy-monitor --services ai-backend,user-services`

---

## Credentials & Safety

- **Datadog**: env vars `CLAUDE_DD_API_KEY`, `CLAUDE_DD_APP_KEY`. Site: `datadoghq.com`
- **AWS**: **ALWAYS use `--profile ai-prod-ro`** for production. NEVER use `ai-prod`.
- **Slack**: Use Slack MCP tools. Channel IDs are fixed (see Step 3 reference).
- Source `~/.zshrc` if env vars are not loaded: `source ~/.zshrc 2>/dev/null`

---

## Architecture: Baseline + Deviation Model

```
Step 0: Parse args, compute intervals
Step 1: Capture baseline snapshot (all 9 categories)
Step 2: Start cron loop
  Each tick:
    - Run checks (parallel agents, batched by frequency tier)
    - Compare against baseline
    - Report ONLY deviations (>20% change or new errors)
    - If all green, one-line "all clear" status
Step 3: On completion, produce final summary report
```

---

## Step 0: Parse Arguments & Setup

Parse `$ARGUMENTS` for duration, interval, environment, services, and skip list.

Defaults:
```
duration = 10m
interval = 2m
env = prod
services = ai-backend, user-services, memory-service, api-gateway
skip = (none)
```

Convert interval to cron expression:
- `1m` → `*/1 * * * *`
- `2m` → `*/2 * * * *`
- `3m` → `*/3 * * * *`
- `5m` → `*/5 * * * *`

Compute `max_ticks = duration / interval`.

---

## Step 1: Capture Baseline Snapshot

Run ALL 9 check categories once in parallel (3 agents, batched). Store results as the baseline for deviation detection.

### Agent 1: Fast Checks (ALB + ECS + Logs)

**1a. ALB (Application Load Balancer)**

Query Datadog v2 timeseries API for the last 5 minutes:
- `aws.applicationelb.httpcode_elb_5xx` — 5xx error count
- `aws.applicationelb.target_response_time.average` — avg response time
- `aws.applicationelb.healthy_host_count` — healthy targets
- `aws.applicationelb.unhealthy_host_count` — unhealthy targets
- `aws.applicationelb.request_count` — request volume

Filter by load balancer tag for the target environment.

**Deviation thresholds:**
- 5xx count > 0 when baseline was 0 → **ALERT**
- Response time > 2x baseline → **WARN**
- Healthy host count decreased → **ALERT**
- Request volume drop > 50% → **WARN** (possible routing issue)

**1b. ECS (Elastic Container Service)**

Query Datadog metrics OR use AWS CLI:
```bash
# Task counts per service
for svc in ai-backend user-services memory-service api-gateway; do
  aws ecs describe-services --profile ai-prod-ro --region ap-south-1 \
    --cluster equalai-prod --services "eai00${svc}00prod" \
    --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,deployments:deployments[*].{status:status,running:runningTaskCount,desired:desiredTaskCount,rollout:rolloutState}}' \
    --output json 2>/dev/null
done
```

**What to capture:**
- Running vs desired task count (mismatch = deploy in progress or crashloop)
- Pending task count (>0 for extended time = stuck)
- Deployment status (PRIMARY vs ACTIVE = rolling deploy in progress)
- Rollout state (COMPLETED, IN_PROGRESS, FAILED)

**Deviation thresholds:**
- Running < desired for >3 minutes → **ALERT**
- Pending > 0 for >5 minutes → **WARN**
- Rollout state = FAILED → **ALERT**
- Task count changed from baseline → **INFO** (expected during deploy)

**1c. Logs (Datadog)**

Query Datadog Logs API for each service, last 2 minutes:
```
POST https://api.datadoghq.com/api/v2/logs/events/search
{
  "filter": {
    "query": "service:<service> status:error",
    "from": "now-2m",
    "to": "now"
  },
  "page": {"limit": 100}
}
```

**What to capture:**
- Error count per service
- Error message fingerprints (group by message template)
- Log volume per service (detect log loss — sudden drop = service down)

**Deviation thresholds:**
- New error types not in baseline → **ALERT**
- Error count > 2x baseline → **WARN**
- Log volume drop > 80% → **ALERT** (service may be down)
- Known noise (pipecat DeprecationWarnings) → ignore, do not report

### Agent 2: Data Layer (RDS + Redis)

**2a. RDS**

Query Datadog v2 timeseries API, 1-min granularity, last 5 minutes:
- `aws.rds.database_connections{dbinstanceidentifier:equalai-prod-db}` — connection count
- `aws.rds.cpuutilization{dbinstanceidentifier:equalai-prod-db}` — CPU %
- `aws.rds.write_latency{dbinstanceidentifier:equalai-prod-db}` — write latency (seconds)
- `aws.rds.read_latency{dbinstanceidentifier:equalai-prod-db}` — read latency (seconds)
- `aws.rds.freeable_memory{dbinstanceidentifier:equalai-prod-db}` — free memory (bytes)
- `aws.rds.free_storage_space{dbinstanceidentifier:equalai-prod-db}` — free disk (bytes)

Also check `equalai-prod-memory-db` with the same metrics.

**Deviation thresholds:**
- Connections change > 20% from baseline → **WARN** (expected during rolling deploy, flag anyway)
- Write latency > 3x baseline → **ALERT**
- CPU > 80% → **ALERT**
- Free storage < 10% provisioned → **WARN**
- Free memory drop > 30% → **WARN**

**2b. Redis (ElastiCache)**

Query Datadog metrics:
- `aws.elasticache.curr_connections` — connection count
- `aws.elasticache.engine_cpu_utilization` — CPU %
- `aws.elasticache.database_memory_usage_percentage` — memory usage %
- `aws.elasticache.evictions` — eviction count
- `aws.elasticache.cache_hit_rate` — hit rate %
- `aws.elasticache.replication_lag` — replication lag (seconds)

Filter by cluster/node tags for the prod environment.

**Deviation thresholds:**
- Memory usage > 85% → **WARN**
- Evictions > 0 when baseline was 0 → **ALERT**
- Hit rate drop > 20% from baseline → **WARN**
- Replication lag > 5s → **ALERT**
- Connections change > 30% → **WARN**

### Agent 3: Messaging + Alerts (SQS + SNS + Slack + DD Monitors)

**3a. SQS**

Query Datadog metrics OR AWS CLI:
```bash
# List queues and check key metrics
aws sqs list-queues --profile ai-prod-ro --region ap-south-1 \
  --queue-name-prefix "equalai-prod" --output json
```

For each queue, check:
- `aws.sqs.approximate_number_of_messages_visible` — queue depth
- `aws.sqs.approximate_age_of_oldest_message` — oldest message age (seconds)
- `aws.sqs.number_of_messages_sent` — throughput
- DLQ depth (queues ending in `-dlq`)

**Deviation thresholds:**
- Queue depth > 10x baseline → **ALERT** (processing stalled)
- Oldest message > 300s → **WARN** (consumer lag)
- DLQ depth > 0 when baseline was 0 → **ALERT** (messages failing)
- Throughput drop > 80% → **WARN** (producer stopped sending)

**3b. SNS**

Query Datadog metrics:
- `aws.sns.number_of_messages_published` — publish count
- `aws.sns.number_of_notifications_failed` — delivery failures

**Deviation thresholds:**
- Delivery failures > 0 when baseline was 0 → **WARN**
- Publish count drop > 50% → **WARN**

**3c. Slack Alerts**

Read latest messages from alert channels:
- `C08KT22MHTJ` — #equal-assistant-alerts
- `C09KTU5M6CA` — #equal-ai-product-alerts

Use Slack MCP tools: `mcp__claude_ai_Slack__slack_read_channel`

**Note:** Datadog sends alerts as Block Kit attachments — message bodies appear empty via MCP. Track message **timestamps** and **count** to detect new alerts. If new alerts detected, also query DD monitors API to identify them.

**3d. Datadog Monitors**

Query triggered monitors:
```
GET https://api.datadoghq.com/api/v1/monitor
```

Filter for `overall_state` in `Alert` or `Warn`. Separate:
- **Pre-existing**: state changed before monitoring started → report once, then ignore
- **New**: state changed during monitoring → **ALERT**
- **Silenced/muted**: report existence but don't flag

---

## Step 2: Start Monitoring Loop

Use `CronCreate` to schedule the recurring prompt:
```
cron: <computed expression>
recurring: true
prompt: <the monitoring check prompt>
```

### Check Frequency Tiers

Not all checks need to run every tick. Batch by frequency:

**Every tick (Tier 1):** ALB 5xx, ECS task count, Logs errors, SQS DLQ, Slack alerts
**Every 3rd tick (Tier 2):** RDS metrics, Redis metrics, SNS metrics, DD monitors full scan

Each tick, spawn **2 parallel agents** (Tier 1 only, or Tier 1 + Tier 2 on every 3rd tick).

### Deviation Reporting

On each tick, compare results against baseline. Output format:

**If all green:**
```
Tick N/M — All green. ALB 0 5xx | ECS 4/4 running | Logs baseline | SQS 0 DLQ | Slack quiet
```

**If deviations found:**
```
Tick N/M — DEVIATIONS DETECTED

| Category | Metric | Baseline | Current | Status |
|----------|--------|----------|---------|--------|
| RDS | Write Latency | 2.5ms | 17.8ms | ALERT — 7x baseline |
| ECS | Connections | 781 | 838 | WARN — rolling deploy |

All other checks: green
```

### Agent Stacking Prevention

If the previous tick's agents are still running when a new tick fires:
- **Do NOT launch new agents** — log "Tick N — previous still running, skipping"
- Wait for the in-flight agents to complete
- Launch new agents on the next tick

This prevents unbounded agent proliferation.

---

## Step 3: Final Summary Report

When monitoring duration expires, cancel the cron via `CronDelete` and produce:

```markdown
## Deployment Monitoring Report

**Duration:** [start time] → [end time] ([N] minutes, [M] ticks)
**Environment:** prod
**Services:** ai-backend, user-services, memory-service, api-gateway

### Timeline of Events
| Time | Event |
|------|-------|
| HH:MM | Monitoring started. Baseline captured. |
| HH:MM | [deviation description] |
| HH:MM | [recovery description] |
| HH:MM | Monitoring complete. |

### Final Status

| Category | Status | Baseline | Final | Notes |
|----------|--------|----------|-------|-------|
| ALB | Green | 0 5xx | 0 5xx | No change |
| ECS | Changed | 8 tasks | 8 tasks | Rolled during deploy, settled |
| Logs | Green | 40 err/2m | 45 err/2m | Within baseline (known noise) |
| RDS | Green | 781 conn, 2.5ms wlat | 450 conn, 2.5ms wlat | Connections changed post-deploy |
| Redis | Green | ... | ... | ... |
| SQS | Green | 0 DLQ | 0 DLQ | ... |
| SNS | Green | ... | ... | ... |
| Slack | Green | Quiet | Quiet | ... |
| DD Monitors | Yellow | 2 pre-existing | 2 pre-existing | [monitor names] |

### Verdict: CLEAN / ISSUES DETECTED

### Action Items (if any)
- [ ] [item requiring follow-up]
```

---

## Guard Rails

- **NEVER use `--profile ai-prod`** — always `ai-prod-ro` (read-only) for production AWS access
- **NEVER modify production state** — this skill is read-only monitoring
- **Graceful degradation** — if a check fails (API error, timeout), skip it and continue with others. Note the skip in the report.
- **Known noise filtering** — pipecat DeprecationWarnings, Azure integration errors, and other known recurring items should be noted once in baseline then suppressed
- **Pre-existing vs new alerts** — always distinguish between monitors that were already alerting before monitoring started vs those that triggered during the monitoring window
- **Auto-cancel on completion** — always `CronDelete` the job when duration expires. Never leave orphaned crons.
- **Concise tick output** — one line if green, table only for deviations. Don't dump raw API responses.

## Error Handling

| Error | Action |
|-------|--------|
| Datadog API returns 403/401 | Report "DD API auth failed — check env vars". Skip DD checks, continue AWS + Slack. |
| Datadog metrics return "Not found" | Try v2 timeseries API instead of v1 query API. If both fail, skip metric. |
| AWS CLI timeout | Retry once. If still failing, skip and note in report. |
| Slack MCP can't read message content | Expected for Datadog Block Kit alerts. Track timestamps + count. Cross-reference with DD monitors API. |
| Service not found in ECS | The service name pattern may differ. Try `eai00<service>00prod` and `equalai-<service>-prod`. |
| Agent stacking (prev tick still running) | Skip current tick. Do NOT launch duplicate agents. |
| Cron fires after duration expires | Cancel cron immediately. Produce final report. |

## Service Name Reference

| Service | DD Service Name | ECS Service Pattern | Production Host |
|---------|----------------|--------------------|-----------------|
| user-services | `user-services` | `eai00user-services00prod` | `user.ai-prod.equal.in` |
| ai-backend | `ai-backend` | `eai00ai-backend00prod` | `backend-2.ai-prod.equal.in` |
| memory-service | `memory-service` | `eai00memory-service00prod` | `memory.ai-prod.equal.in` |
| api-gateway | `api-gateway` | `eai00api-gateway00prod` | `business-api.equal.in` |

## Slack Channel Reference

| Channel | ID | Purpose |
|---------|-----|---------|
| #equal-assistant-alerts | `C08KT22MHTJ` | Datadog monitor alerts |
| #equal-ai-product-alerts | `C09KTU5M6CA` | Product-level alerts |
