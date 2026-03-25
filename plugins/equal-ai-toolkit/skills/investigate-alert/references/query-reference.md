# Query Reference

Quick reference for Datadog, AWS CloudWatch, and database queries used during investigations.

## Datadog MCP Tools

### Logs
```
mcp__datadog__get_logs
  query: "service:{tag} @levelname:ERROR"
  timeRange: "past_15_minutes" | "past_1_hour" | "past_4_hours"
  limit: 50
```

Common log query patterns:
- All errors: `service:{tag} @levelname:ERROR`
- HTTP 5xx: `service:{tag} @http.status_code:>=500`
- Specific error: `service:{tag} @levelname:ERROR "{error_message}"`
- Session drill-down: `service:{tag} @session_id:{id}`
- Trace correlation: `service:{tag} @trace_id:{id}`
- Exclude noise: `service:{tag} @levelname:ERROR -"health check" -"readiness"`

### Monitors
```
mcp__datadog__get_monitors
  groupStates: "alert,warn"
```

### Metrics
```
mcp__datadog__query_metrics
  query: "{metric_query}"
  timeRange: "past_15_minutes"
```

Common metric queries:
- Error rate: `sum:trace.fastapi.request.errors{service:{tag}}.as_rate()`
- Request rate: `sum:trace.fastapi.request.hits{service:{tag}}.as_rate()`
- P95 latency: `avg:trace.fastapi.request.duration.by.service.95p{service:{tag}}`
- P99 latency: `avg:trace.fastapi.request.duration.by.service.99p{service:{tag}}`
- Custom metrics: `avg:myequal.{metric_name}{service:{tag}}`

### Traces
```
mcp__datadog__list_traces
  query: "service:{tag}"
  timeRange: "past_15_minutes"
  limit: 20
```

### RUM (if investigating user-facing issues)
```
mcp__datadog__get_rum_events
  query: "@type:error"
  timeRange: "past_1_hour"
```

## AWS CloudWatch Queries

All AWS CLI commands use: `aws --profile {profile} --region {region} --output json`

### ECS

```bash
# Service status
aws ecs describe-services \
  --cluster {ecs_cluster} \
  --services {ecs_service_name}

# Running tasks
aws ecs list-tasks \
  --cluster {ecs_cluster} \
  --service-name {ecs_service_name}

# Task details (stopped tasks for crash investigation)
aws ecs describe-tasks \
  --cluster {ecs_cluster} \
  --tasks {task_arn}

# Recent events (deployments, failures)
# (included in describe-services output, events field)
```

### ALB

```bash
# Target group health
aws elbv2 describe-target-health \
  --target-group-arn {target_group_arn}

# ALB metrics
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_Target_5XX_Count \
  --dimensions Name=LoadBalancer,Value={alb_cloudwatch_suffix} \
  --start-time {iso_start} \
  --end-time {iso_end} \
  --period 60 \
  --statistics Sum
```

Key ALB metrics:
- `HTTPCode_Target_5XX_Count` — 5xx from your services
- `HTTPCode_ELB_5XX_Count` — 5xx from the ALB itself (different!)
- `TargetResponseTime` — latency
- `RequestCount` — throughput
- `ActiveConnectionCount` — concurrent connections
- `UnHealthyHostCount` — targets marked unhealthy
- `HealthyHostCount` — healthy targets

### RDS

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name {metric_name} \
  --dimensions Name=DBInstanceIdentifier,Value={db_instance_id} \
  --start-time {iso_start} \
  --end-time {iso_end} \
  --period 60 \
  --statistics Average
```

Key RDS metrics:
- `DatabaseConnections` — active connection count (compare to pool max)
- `CPUUtilization` — database CPU
- `ReadLatency` / `WriteLatency` — I/O latency (seconds)
- `FreeableMemory` — available RAM (bytes)
- `FreeStorageSpace` — disk space (bytes)
- `ReplicaLag` — read replica lag (seconds, only on replicas)
- `DiskQueueDepth` — I/O queue depth (high = I/O bottleneck)

### ElastiCache (Redis)

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ElastiCache \
  --metric-name {metric_name} \
  --dimensions Name=CacheClusterId,Value={cluster_id} \
  --start-time {iso_start} \
  --end-time {iso_end} \
  --period 60 \
  --statistics Average
```

Key Redis metrics:
- `CurrConnections` — current connection count
- `EngineCPUUtilization` — Redis engine CPU
- `CacheHitRate` — hit ratio (low = inefficient caching)
- `Evictions` — keys evicted (cache full)
- `ReplicationLag` — replica lag (seconds)
- `DatabaseMemoryUsagePercentage` — memory utilization

## Deep Link URL Formats

### Datadog Logs
```
https://app.datadoghq.com/logs?query={URL_ENCODED_QUERY}&from_ts={EPOCH_MS}&to_ts={EPOCH_MS}
```

### Datadog Metrics Explorer
```
https://app.datadoghq.com/metric/explorer?exp_metric={METRIC}&exp_scope={SCOPE}&start={EPOCH_SEC}&end={EPOCH_SEC}
```

### Datadog APM Traces
```
https://app.datadoghq.com/apm/traces?query=service%3A{SERVICE_TAG}&start={EPOCH_SEC}&end={EPOCH_SEC}
```

### CloudWatch Metrics
```
https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}#metricsV2:graph=~(metrics~(~(~'{NAMESPACE}~'{METRIC}~'{DIM_NAME}~'{DIM_VALUE}))~period~60~start~'{ISO_START}~end~'{ISO_END})
```

### ECS Service Console
```
https://{REGION}.console.aws.amazon.com/ecs/v2/clusters/{CLUSTER}/services/{SERVICE}/health?region={REGION}
```

## Database Queries (Read-Only)

Connection pattern from config:
```
postgresql://{username}:{password}@{host}:5432/{db_name}
```

Always use the read replica for investigation queries when available. Never run queries against the primary during an active incident unless the read replica is suspected to be the issue.

Useful investigation queries:
```sql
-- Active connections by state
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;

-- Long-running queries (> 30 seconds)
SELECT pid, now() - pg_stat_activity.query_start AS duration, query
FROM pg_stat_activity
WHERE state != 'idle' AND now() - pg_stat_activity.query_start > interval '30 seconds'
ORDER BY duration DESC;

-- Lock contention
SELECT blocked_locks.pid AS blocked_pid,
       blocking_locks.pid AS blocking_pid,
       blocked_activity.query AS blocked_query
FROM pg_catalog.pg_locks blocked_locks
JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
JOIN pg_catalog.pg_locks blocking_locks ON blocking_locks.locktype = blocked_locks.locktype
WHERE NOT blocked_locks.granted;

-- Table sizes (for storage issues)
SELECT relname, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 10;
```
