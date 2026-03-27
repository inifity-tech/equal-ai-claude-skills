# Production Query Reference for Tech Advisor

This reference provides the exact query patterns, service tags, and database access patterns needed for the production observability and data layer validation subagents.

## Configuration

Read `.claude/config/toolkit-config.yaml` in the project root first. It contains:
- `services[].datadog_tag` — the Datadog service tag for each service
- `services[].database` — the database name for each service
- `databases.primary.host` / `databases.primary.read_replica` — DB hosts
- `databases.primary.connection_pattern` — how to construct connection strings

If the config doesn't exist, ask the user — don't guess infrastructure names.

## Datadog Log Queries

### Tool: `mcp__datadog__get_logs`

```
mcp__datadog__get_logs
  query: "<query string>"
  timeRange: "past_15_minutes" | "past_1_hour" | "past_4_hours" | "past_1_day" | "past_7_days"
  limit: 50
```

### Service Tag Mapping

Use the `datadog_tag` from toolkit-config.yaml. Common pattern: `service:{datadog_tag}`

### Query Patterns by Use Case

**Error discovery:**
```
service:{tag} @levelname:ERROR
service:{tag} @levelname:ERROR -"health check" -"readiness"
service:{tag} @http.status_code:>=500
```

**Performance investigation:**
```
service:{tag} @levelname:WARNING "slow"
service:{tag} "timeout"
service:{tag} "connection pool" OR "pool exhausted"
service:{tag} "retry" OR "backoff"
```

**Specific component investigation:**
```
service:{tag} "websocket" @levelname:ERROR
service:{tag} "sqs" OR "sns" @levelname:ERROR
service:{tag} "redis" @levelname:ERROR
service:{tag} "database" OR "sqlalchemy" @levelname:ERROR
```

**Session/request drill-down:**
```
service:{tag} @session_id:{id}
service:{tag} @trace_id:{id}
```

### Log Field Reference

Equal AI services use structured JSON logging. Common fields:
- `@levelname` — log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `@http.status_code` — HTTP response code
- `@session_id` — call session identifier
- `@trace_id` — Datadog APM trace ID for correlation
- `@duration` — operation duration (when instrumented)
- `@caller_id` — caller identifier
- `@service` — service name

## Datadog Metric Queries

### Tool: `mcp__datadog__query_metrics`

```
mcp__datadog__query_metrics
  query: "<metric query>"
  timeRange: "past_15_minutes" | "past_1_hour" | "past_4_hours" | "past_1_day"
```

### Common Metric Patterns

**Request performance:**
```
sum:trace.fastapi.request.errors{service:{tag}}.as_rate()
sum:trace.fastapi.request.hits{service:{tag}}.as_rate()
avg:trace.fastapi.request.duration.by.service.95p{service:{tag}}
avg:trace.fastapi.request.duration.by.service.99p{service:{tag}}
```

**Custom Equal AI metrics** (prefixed with `myequal.`):
```
avg:myequal.{metric_name}{service:{tag}}
```

Examples:
- `avg:myequal.websocket.packet_processing.ms{service:backend}` — audio packet latency
- `avg:myequal.sqs.processing.duration.ms{service:post-processing}` — SQS message processing time
- `sum:myequal.sqs.messages.processed{service:memory-service}.as_count()` — SQS throughput
- `avg:myequal.redis.operation.duration.ms{service:backend}` — Redis operation latency
- `avg:myequal.db.query.duration.ms{service:user-services}` — DB query latency

**Infrastructure metrics:**
```
avg:aws.rds.database_connections{dbinstanceidentifier:{db_instance}}
avg:aws.rds.cpuutilization{dbinstanceidentifier:{db_instance}}
avg:aws.elasticache.curr_connections{cacheclusterid:{cluster_id}}
avg:aws.sqs.approximate_number_of_messages_visible{queuename:{queue_name}}
avg:aws.sqs.approximate_age_of_oldest_message{queuename:{queue_name}}
```

## Datadog Traces

### Tool: `mcp__datadog__list_traces`

```
mcp__datadog__list_traces
  query: "service:{tag}"
  timeRange: "past_15_minutes"
  limit: 20
```

Useful trace queries:
- Slow requests: `service:{tag} @duration:>1s`
- Error traces: `service:{tag} @http.status_code:>=500`
- Specific endpoint: `service:{tag} resource_name:"{method} {path}"`

## Datadog Monitors

### Tool: `mcp__datadog__get_monitors`

```
mcp__datadog__get_monitors
  groupStates: "alert,warn"
```

This returns all monitors currently in alert or warning state. Check what monitoring exists and what's missing for the topic under discussion.

## Database Access

### Connection

Use the connection details from toolkit-config.yaml:
- **Always use the read replica** (`databases.primary.read_replica`) for investigation queries
- Never modify data — SELECT only
- Keep queries lightweight — avoid full table scans on large tables

### How to Query

Run SQL via bash using `psql`:
```bash
psql "postgresql://{user}:{password}@{read_replica_host}:5432/{db_name}" -c "SELECT ..."
```

Or for multi-line queries:
```bash
psql "postgresql://{user}:{password}@{read_replica_host}:5432/{db_name}" << 'SQL'
SELECT ...
SQL
```

### Schema Discovery Queries

```sql
-- List all tables in a database
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;

-- Get column details for a table
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = '{table_name}' AND table_schema = 'public'
ORDER BY ordinal_position;

-- List all indexes on a table
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = '{table_name}'
ORDER BY indexname;

-- Check foreign keys
SELECT tc.constraint_name, tc.table_name, kcu.column_name,
       ccu.table_name AS foreign_table, ccu.column_name AS foreign_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = '{table_name}';
```

### Data Layer Validation Queries

```sql
-- Table row counts (for understanding scale)
SELECT relname, n_live_tup AS row_count
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC;

-- Table sizes (for storage and index assessment)
SELECT relname,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       pg_size_pretty(pg_relation_size(relid)) AS data_size,
       pg_size_pretty(pg_total_relation_size(relid) - pg_relation_size(relid)) AS index_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;

-- Check for missing indexes (sequential scans on large tables)
SELECT relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch
FROM pg_stat_user_tables
WHERE seq_scan > 100 AND n_live_tup > 10000
ORDER BY seq_tup_read DESC;

-- Null rate analysis (understand data completeness)
-- Run for specific columns of interest:
SELECT
  COUNT(*) AS total,
  COUNT(column_name) AS non_null,
  ROUND(100.0 * COUNT(column_name) / COUNT(*), 1) AS fill_rate_pct
FROM {table_name};

-- Active connections and their state
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;

-- Connection pool pressure
SELECT count(*) AS total_connections,
       count(*) FILTER (WHERE state = 'active') AS active,
       count(*) FILTER (WHERE state = 'idle') AS idle,
       count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction
FROM pg_stat_activity
WHERE datname = current_database();

-- Long-running queries
SELECT pid, now() - pg_stat_activity.query_start AS duration, query, state
FROM pg_stat_activity
WHERE state != 'idle' AND now() - pg_stat_activity.query_start > interval '5 seconds'
ORDER BY duration DESC;

-- Index usage statistics
SELECT indexrelname, idx_scan, idx_tup_read, idx_tup_fetch
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY idx_scan DESC;
```

### Cross-Validating Code Models Against DB Schema

To validate that SQLModel/SQLAlchemy model definitions match the actual DB:

1. Read the model file in code (e.g., `app/models/user.py`)
2. Run the schema discovery query for that table
3. Compare:
   - Column names and types match?
   - Nullable settings match?
   - Indexes defined in code exist in DB?
   - Any columns in DB not in code (leftover from old migrations)?
   - Any columns in code not in DB (unmigrated)?
