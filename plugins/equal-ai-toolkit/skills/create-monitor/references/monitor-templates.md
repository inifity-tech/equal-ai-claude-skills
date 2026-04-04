# Datadog Monitor API Templates

Reference for creating monitors via the Datadog v1 Monitor API.

**Base URL:** `https://api.datadoghq.com/api/v1/monitor`
**Auth headers:**
```
DD-API-KEY: ${CLAUDE_DD_API_KEY}
DD-APPLICATION-KEY: ${CLAUDE_DD_APP_KEY}
```

---

## Table of Contents

1. [Log Monitor](#log-monitor)
2. [Metric Monitor](#metric-monitor)
3. [APM Monitor](#apm-monitor)
4. [Composite Monitor](#composite-monitor)
5. [Common Options](#common-options)
6. [Template Variables](#template-variables)
7. [Notification Syntax](#notification-syntax)

---

## Log Monitor

Triggers when a log query exceeds a threshold within an evaluation window.

**Type:** `log alert`

**Query format:**
```
logs("search_query").index("*").rollup("count").by("tag_key").last("evaluation_window") > threshold
```

**Rollup functions:** `count`, `avg`, `max`, `min`, `sum`, `cardinality`
**Evaluation windows:** `1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `2h`, `4h`, `24h`

### Example: Error count monitor

```json
{
  "name": "[Equal AI] High error rate on ai-backend",
  "type": "log alert",
  "query": "logs(\"service:ai-backend status:error\").index(\"*\").rollup(\"count\").last(\"5m\") > 50",
  "message": "## High error rate on ai-backend\n\n**Service:** ai-backend | **Env:** {{env.name}}\n\n...\n\n@slack-equal-assistant-alerts",
  "tags": ["service:ai-backend", "env:prod", "team:equal-ai", "created-by:claude-monitor-skill"],
  "options": {
    "thresholds": {
      "critical": 50,
      "warning": 30
    },
    "notify_no_data": true,
    "no_data_timeframe": 10,
    "renotify_interval": 60,
    "escalation_message": "Still firing: {{value}} errors in the last 5m on ai-backend. {{monitor_link}}",
    "include_tags": true,
    "enable_logs_sample": true,
    "groupby_simple_monitor": true
  }
}
```

### Example: Specific log pattern monitor

```json
{
  "name": "[Equal AI] Connection timeout errors on user-services",
  "type": "log alert",
  "query": "logs(\"service:user-services \\\"ConnectionError\\\" OR \\\"TimeoutError\\\"\").index(\"*\").rollup(\"count\").last(\"10m\") > 10",
  "message": "...",
  "tags": ["service:user-services", "env:prod", "team:equal-ai", "created-by:claude-monitor-skill"],
  "options": {
    "thresholds": {
      "critical": 10,
      "warning": 5
    },
    "notify_no_data": false,
    "renotify_interval": 60,
    "include_tags": true,
    "enable_logs_sample": true
  }
}
```

### Log query syntax reference

| Operator | Example | Notes |
|----------|---------|-------|
| Exact match | `service:ai-backend` | Tag or facet match |
| Keyword | `"TimeoutError"` | Searches message body |
| OR | `status:error OR status:critical` | Either condition |
| AND | `service:ai-backend status:error` | Space = implicit AND |
| NOT | `-status:info` | Exclude |
| Wildcard | `@http.url:*/api/v1/*` | Glob matching |
| Range | `@duration:>5000000000` | Numeric range (nanoseconds) |
| Facet | `@error.kind:ConnectionError` | Structured facet match |

---

## Metric Monitor

Triggers when a metric crosses a threshold.

**Type:** `metric alert`  (use `query alert` in the `type` field)

**Query format:**
```
aggregator(last_window):metric_function:metric.name{tag:value} operator threshold
```

**Aggregators:** `avg`, `max`, `min`, `sum`, `change`, `pct_change`
**Windows:** `last_1m`, `last_5m`, `last_10m`, `last_15m`, `last_30m`, `last_1h`, `last_4h`
**Functions:** `avg`, `max`, `min`, `sum`, `count` (applied per timeseries point)

### Example: High latency monitor

```json
{
  "name": "[Equal AI] High p95 latency on api-gateway",
  "type": "query alert",
  "query": "avg(last_5m):avg:trace.fastapi.request.duration.by.resource_name.95p{service:api-gateway,env:prod} > 2",
  "message": "## High p95 latency on api-gateway\n\n...\n\n@slack-equal-assistant-alerts",
  "tags": ["service:api-gateway", "env:prod", "team:equal-ai", "created-by:claude-monitor-skill"],
  "options": {
    "thresholds": {
      "critical": 2,
      "warning": 1.5
    },
    "notify_no_data": true,
    "no_data_timeframe": 10,
    "renotify_interval": 60,
    "include_tags": true
  }
}
```

### Example: ECS task count monitor

```json
{
  "name": "[Equal AI] ECS task count drop on ai-backend",
  "type": "query alert",
  "query": "avg(last_5m):avg:aws.ecs.service.running{servicename:eai00ai-backend00prod} < 2",
  "message": "...",
  "tags": ["service:ai-backend", "env:prod", "team:equal-ai", "created-by:claude-monitor-skill"],
  "options": {
    "thresholds": {
      "critical": 2,
      "warning": 3
    },
    "notify_no_data": true,
    "no_data_timeframe": 10
  }
}
```

### Example: RDS connection count monitor

```json
{
  "name": "[Equal AI] High RDS connections on equalai-prod-db",
  "type": "query alert",
  "query": "avg(last_5m):avg:aws.rds.database_connections{dbinstanceidentifier:equalai-prod-db} > 200",
  "message": "...",
  "tags": ["env:prod", "team:equal-ai", "created-by:claude-monitor-skill"],
  "options": {
    "thresholds": {
      "critical": 200,
      "warning": 150
    },
    "notify_no_data": true,
    "no_data_timeframe": 10
  }
}
```

### Common metric paths

| What to monitor | Metric name |
|----------------|-------------|
| ECS CPU | `aws.ecs.service.cpuutilization` |
| ECS Memory | `aws.ecs.service.memory_utilization` |
| ECS Running tasks | `aws.ecs.service.running` |
| RDS CPU | `aws.rds.cpuutilization` |
| RDS Connections | `aws.rds.database_connections` |
| RDS Write latency | `aws.rds.write_latency` |
| RDS Read latency | `aws.rds.read_latency` |
| RDS Free memory | `aws.rds.freeable_memory` |
| Redis CPU | `aws.elasticache.engine_cpu_utilization` |
| Redis Memory % | `aws.elasticache.database_memory_usage_percentage` |
| Redis Evictions | `aws.elasticache.evictions` |
| Redis Connections | `aws.elasticache.curr_connections` |
| ALB 5xx | `aws.applicationelb.httpcode_elb_5xx` |
| ALB Response time | `aws.applicationelb.target_response_time.average` |
| ALB Request count | `aws.applicationelb.request_count` |
| SQS Queue depth | `aws.sqs.approximate_number_of_messages_visible` |
| SQS DLQ depth | `aws.sqs.approximate_number_of_messages_visible` (filter by DLQ queue name) |
| SQS Oldest message | `aws.sqs.approximate_age_of_oldest_message` |
| SNS Publish count | `aws.sns.number_of_messages_published` |
| SNS Delivery failures | `aws.sns.number_of_notifications_failed` |

---

## APM Monitor

Triggers on APM trace metrics like latency, error rate, or throughput.

**Type:** `query alert` (uses APM metric queries)

**Query format for error rate:**
```
sum(last_5m):sum:trace.{operation}.errors{service:name,env:prod}.as_count() / sum:trace.{operation}.hits{service:name,env:prod}.as_count() > 0.05
```

**Query format for latency:**
```
avg(last_5m):avg:trace.{operation}.duration.by.resource_name.95p{service:name,env:prod} > threshold_seconds
```

### Example: High error rate on specific endpoint

```json
{
  "name": "[Equal AI] High error rate on /api/v1/calls endpoint",
  "type": "query alert",
  "query": "sum(last_10m):sum:trace.fastapi.request.errors{service:ai-backend,resource_name:/api/v1/calls,env:prod}.as_count() / sum:trace.fastapi.request.hits{service:ai-backend,resource_name:/api/v1/calls,env:prod}.as_count() > 0.1",
  "message": "...",
  "tags": ["service:ai-backend", "env:prod", "team:equal-ai", "created-by:claude-monitor-skill"],
  "options": {
    "thresholds": {
      "critical": 0.1,
      "warning": 0.05
    },
    "notify_no_data": false,
    "renotify_interval": 60,
    "include_tags": true
  }
}
```

---

## Composite Monitor

Combines multiple existing monitors with boolean logic. Useful for reducing alert noise — e.g., only alert if BOTH error rate is high AND latency is high.

**Type:** `composite`

**Query format:**
```
monitor_id_1 && monitor_id_2
monitor_id_1 || monitor_id_2
!monitor_id_1
```

### Example

```json
{
  "name": "[Equal AI] ai-backend degraded (errors + latency)",
  "type": "composite",
  "query": "12345678 && 87654321",
  "message": "Both error rate AND latency monitors are firing for ai-backend. This indicates a significant service degradation, not just noise.\n\n@slack-equal-assistant-alerts",
  "tags": ["service:ai-backend", "env:prod", "team:equal-ai", "created-by:claude-monitor-skill"],
  "options": {
    "renotify_interval": 60
  }
}
```

---

## Common Options

These options apply to all monitor types:

```json
{
  "options": {
    "thresholds": {
      "critical": 50,
      "warning": 30,
      "ok": 10
    },
    "notify_no_data": true,
    "no_data_timeframe": 10,
    "renotify_interval": 60,
    "escalation_message": "Still firing. {{value}} | {{monitor_link}}",
    "include_tags": true,
    "require_full_window": false,
    "evaluation_delay": 60,
    "new_group_delay": 60,
    "notify_audit": false,
    "enable_logs_sample": true
  }
}
```

| Option | Purpose | Recommendation |
|--------|---------|----------------|
| `notify_no_data` | Alert if data stops flowing | `true` for infrastructure; `false` for rare log patterns |
| `no_data_timeframe` | Minutes before no-data alert | 10 for most monitors |
| `renotify_interval` | Minutes between re-notifications | 60 (don't over-notify) |
| `require_full_window` | Wait for full eval window | `false` for faster alerting |
| `evaluation_delay` | Seconds to wait for data arrival | 60 for AWS CloudWatch metrics (they lag) |
| `new_group_delay` | Delay for new tag groups | 60 to avoid alert storms on deploy |
| `enable_logs_sample` | Include log samples in notification | `true` for log monitors |

---

## Template Variables

Use these in alert messages — Datadog replaces them at alert time:

| Variable | Value |
|----------|-------|
| `{{value}}` | Current metric value that triggered the alert |
| `{{threshold}}` | The threshold that was breached |
| `{{warn_threshold}}` | Warning threshold |
| `{{monitor_link}}` | URL to the monitor page |
| `{{last_triggered_at}}` | Timestamp of the alert |
| `{{last_triggered_at_epoch}}` | Epoch timestamp |
| `{{env.name}}` | Environment tag value |
| `{{host.name}}` | Host name (if applicable) |
| `{{comparator}}` | The comparator used (>, <, etc.) |
| `{{#is_alert}}...{{/is_alert}}` | Content shown only when in alert state |
| `{{#is_warning}}...{{/is_warning}}` | Content shown only when in warning state |
| `{{#is_recovery}}...{{/is_recovery}}` | Content shown on recovery |
| `{{#is_no_data}}...{{/is_no_data}}` | Content shown on no-data |

---

## Notification Syntax

### Slack
- Channel: `@slack-{channel-name}` (e.g., `@slack-equal-assistant-alerts`)
- User: `@slack-{workspace}-{username}`

### Conditional notifications
```
{{#is_alert}}
@slack-equal-assistant-alerts
Critical: Error rate is {{value}} (threshold: {{threshold}})
{{/is_alert}}

{{#is_warning}}
Warning: Error rate is {{value}} (threshold: {{warn_threshold}})
{{/is_warning}}

{{#is_recovery}}
Recovered: Error rate is back to normal at {{value}}
{{/is_recovery}}
```

### Recovery message
Include a recovery notification so the team knows when things are back to normal. Use the `{{#is_recovery}}` block to customize what the recovery message says.
