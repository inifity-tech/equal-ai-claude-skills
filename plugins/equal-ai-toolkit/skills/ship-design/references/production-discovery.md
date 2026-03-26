# Production Discovery Reference

Before designing, ground the design in reality by querying production systems.

## 1. Code & Architecture Discovery

### Read existing architecture docs
```
Read /Users/akshay/<repo>/docs/architecture/HLD.md
Read /Users/akshay/<repo>/docs/architecture/LLD-*.md
```

### Trace current code paths
Use Grep and Read across affected repos to understand:
- Current request flow (API → service → manager → DB)
- Current event flow (publisher → SNS → SQS → consumer)
- Current data models and relationships

### Map dependencies
For each affected service, identify:
- Outbound HTTP calls (grep for `httpx`, `aiohttp`, `requests`)
- SNS publishers (grep for `sns_client`, `publish`, `TopicArn`)
- SQS consumers (grep for `sqs_client`, `receive_message`, `consumer`)
- Redis usage (grep for `redis_client`, `RedisStream`)
- DynamoDB usage (grep for `dynamodb`, `Table`)
- S3 usage (grep for `s3_client`, `upload_file`, `get_object`)

## 2. Production Logs Analysis

### Traffic patterns (last 7 days)
```
mcp__dd__get_logs(query="service:<service> <flow_keyword>", from="-7d", to="now", limit=100)
```

### Trace analysis
```
mcp__dd__list_traces(query="service:<service> resource_name:<endpoint>", from="-7d", to="now", limit=50)
```

### Key metrics to extract:
- **Request volume**: How many requests/day on affected endpoints?
- **Error rate**: What's the current error rate? Any existing issues?
- **Latency**: p50, p95, p99 for affected endpoints
- **Dependencies**: Which services actually call each other? (trace data reveals this)

### Useful Datadog queries:
| What | Query |
|------|-------|
| All traffic to endpoint | `service:<svc> resource_name:<endpoint>` |
| Errors only | `service:<svc> status:error` |
| Slow requests | `service:<svc> @duration:>1000000000` (ns) |
| Cross-service calls | `service:<svc> @http.url:*<other-svc>*` |
| Event processing | `service:<svc> "event" OR "consumer" OR "processed"` |

## 3. Database State Analysis

### Connect to production read replica
```bash
psql "$PPS_PROD_DB_URL"
```

### Queries to run:
```sql
-- Table sizes and row counts
SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_stat_user_tables
WHERE relname IN ('<table1>', '<table2>')
ORDER BY n_live_tup DESC;

-- Current schema
\d+ <tablename>

-- Index usage
SELECT indexrelname, idx_scan, idx_tup_read, idx_tup_fetch
FROM pg_stat_user_indexes
WHERE relname = '<tablename>';

-- Data distribution (for planning)
SELECT <grouping_column>, COUNT(*)
FROM <tablename>
GROUP BY <grouping_column>
ORDER BY COUNT(*) DESC
LIMIT 20;

-- Growth rate (if created_at exists)
SELECT date_trunc('day', created_at) AS day, COUNT(*)
FROM <tablename>
WHERE created_at > NOW() - INTERVAL '30 days'
GROUP BY day
ORDER BY day;
```

### Important: SQLModel table naming
SQLModel auto-generates table names by lowercasing the class name without underscores:
- `CallLog` → `calllog` (NOT `call_log`)
- `UserProfile` → `userprofile`
- Unless `__tablename__` is explicitly set in the model

## 4. Cross-Repo Dependency Mapping

Build a dependency graph from actual production evidence:

### From code (static analysis):
- HTTP clients in each service → which services they call
- Event publishers → which topics/queues they write to
- Event consumers → which queues they read from

### From Datadog traces (runtime evidence):
- Service map: `mcp__dd__list_traces(query="service:<svc>")` → inspect upstream/downstream
- Verify that code-level dependencies match actual runtime dependencies
- Identify any undocumented dependencies

### Output: Current State Summary

Structure the findings as:

```markdown
## Current State Summary

### Architecture (from code + traces)
- <Service A> calls <Service B> via HTTP for <purpose>
- <Service A> publishes to <SNS topic> consumed by <Service C>
- <Service B> reads/writes <DynamoDB table>

### Production Baselines
| Metric | Value | Source |
|--------|-------|--------|
| Daily requests to <endpoint> | ~10K | Datadog |
| p99 latency <endpoint> | 450ms | Datadog |
| Error rate <service> | 0.1% | Datadog |
| <table> row count | 2.5M | DB |
| <table> daily growth | ~5K rows | DB |

### Dependency Map
<Mermaid diagram showing service interactions>

### Known Issues
- <Any existing issues discovered during analysis>
```

This summary becomes input to the design agents in Step 3.
