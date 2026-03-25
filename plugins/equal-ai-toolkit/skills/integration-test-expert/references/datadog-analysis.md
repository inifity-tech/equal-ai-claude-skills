# Datadog Analysis Reference

## MCP Tools Available

### Traces
Use `mcp__datadog__list_traces` to discover all active endpoints:
- Filter by service name and time range
- Group by resource name to get endpoint list
- Look at span metadata for downstream calls (db, cache, http)

### Logs
Use `mcp__datadog__get_logs` with queries like:
- `service:{service_name}` — all logs
- `service:{service_name} @levelname:ERROR` — error logs only
- `service:{service_name} @http.url:*/api/v1/*` — API request logs
- `service:{service_name} @duration:>5000` — slow operations

### Metrics
Use `mcp__datadog__query_metrics` for:
- `avg:trace.http.request.duration{service:{service_name}} by {resource_name}` — latency per endpoint
- `sum:trace.http.request.hits{service:{service_name}} by {resource_name}.as_count()` — throughput
- `sum:trace.http.request.errors{service:{service_name}} by {resource_name}.as_count()` — errors

### Monitors
Use `mcp__datadog__get_monitors` to find:
- What the team considers critical (these flows need integration tests first)
- Alert thresholds (useful for setting test assertions on latency)

## Analysis Template

After gathering data, structure findings as:

```json
{
  "service": "service-name",
  "analysis_period": "last 7 days",
  "endpoints": [
    {
      "method": "POST",
      "path": "/api/v1/resource",
      "avg_latency_ms": 250,
      "p99_latency_ms": 1200,
      "requests_per_hour": 500,
      "error_rate_pct": 0.5,
      "dependencies": ["postgresql", "redis"],
      "key_log_patterns": [
        "Processing request {request_id}",
        "Operation completed in {duration}ms"
      ]
    }
  ],
  "critical_monitors": [
    {
      "name": "Service High Error Rate",
      "query": "...",
      "threshold": "5%"
    }
  ]
}
```

## Mapping Traces to Code

For each traced endpoint, identify:
1. The FastAPI/Flask route handler (match HTTP method + path)
2. The dependency chain from trace spans (each span = a function/service call)
3. Database spans show which tables are queried
4. Redis spans show which keys are accessed
5. HTTP spans show external service calls

This trace-to-code mapping directly informs what each integration test needs to set up and verify.

## Priority Matrix

Prioritize test coverage based on:

| Factor | Weight | Reason |
|--------|--------|--------|
| Traffic volume | High | Most-used endpoints have highest impact |
| Error rate | High | Flaky endpoints need test coverage |
| Business criticality | High | Revenue/user-facing features |
| Dependency count | Medium | More dependencies = more failure modes |
| Recent changes | Medium | New code needs validation |
