---
name: ship-test
description: Design-driven E2E testing — boots real services against staging infra, executes E2E-* specs from design docs, validates DB state + SQS events + logs. Independently invocable or callable from /ship-implement.
disable-model-invocation: false
---

# /ship-test — Design-Driven Multi-Service E2E Testing

Runs real end-to-end tests by booting multiple services locally against staging infrastructure. Test scenarios come from the design document's E2E specs, not from guesswork.

**Difference from `/test`**: `/test` handles single-service testing for branch changes. `/ship-test` handles **multi-service cross-cutting flows** driven by design doc specifications — the kind that validate an entire feature across service boundaries.

## Parameters

`$ARGUMENTS` can include:
- Jira ticket ID (e.g., `EAI-1306`) — required, used to find design doc and feature branches
- `--services <list>` — override which services to boot (default: auto-detect from design)
- `--scenarios <list>` — run specific E2E scenario IDs only (e.g., `--scenarios E2E-1,E2E-3`)
- `--skip-setup` — skip service boot (services already running from a previous run)
- `--skip-cleanup` — leave services running after tests (for debugging)
- `--max-fix-cycles <N>` — max test→fix→retest iterations (default: 3)

Example: `/ship-test EAI-1306`, `/ship-test EAI-1306 --scenarios E2E-1,E2E-2`

---

## Step 0: Gather Context

### 0.1 Find the Design Document

Search Confluence for the design doc linked to the ticket:

```
Tool: mcp__claude_ai_Atlassian__searchConfluenceUsingCql
CQL: space.key = "team1c8727e1bc6842f5b0ab88d80097ad17" AND text ~ "<TICKET-ID>"
```

Read the design doc. Extract:
- **E2E test scenarios** — look for sections named `E2E-*`, `## E2E Tests`, `## Test Scenarios`, or `## Acceptance Tests`
- **API contracts** — endpoints, request/response schemas
- **Event schemas** — SQS/SNS message formats, CloudEvents payloads
- **Data models** — expected DB state after flows
- **Service interaction diagram** — which services talk to which

If no E2E scenarios are defined in the design doc, generate them from the API contracts and event flows. Flag this to the user: "Design doc has no E2E specs — generated scenarios from API contracts."

### 0.2 Identify Affected Services & Branches

From the ticket ID, find feature branches across all repos:

```bash
for repo in myequal-ai-backend myequal-ai-user-services memory-service myequal-ai-cdk myequal-ai-lambdas myequal-post-processing-service myequal-api-gateway; do
  branch=$(cd ~/myequal-ai-${repo} 2>/dev/null || cd ~/${repo} 2>/dev/null && git branch -r --list "*${TICKET_ID}*" 2>/dev/null | head -1 | tr -d ' ')
  if [ -n "$branch" ]; then
    echo "${repo}: ${branch}"
  fi
done
```

Also check open PRs:
```bash
for repo in myequal-ai-backend myequal-ai-user-services memory-service myequal-post-processing-service myequal-api-gateway; do
  gh pr list --repo inifity-tech/$repo --state open --search "<TICKET-ID>" --json number,headRefName --jq '.[] | "\(.headRefName)"'
done
```

### 0.3 Build Service Matrix

Determine which services need to run locally:

| Service | Branch | Port | Needed? |
|---------|--------|------|---------|
| user-services | feature branch or master | 8000 | If in test flow |
| ai-backend | feature branch or master | 8001 | If in test flow |
| memory-service | feature branch or master | 8002 | If in test flow |

**Rules:**
- Services with code changes → run on feature branch
- Services needed for the flow but unchanged → run on master
- Services not in the test flow → skip
- Lambda functions → tested via direct invocation, not as local servers
- CDK → verify resource names only, no runtime

---

## Step 1: Setup Test Environment

Read the multi-service testing reference for detailed setup:
```
Read ~/.claude/skills/ship-test/references/multi-service-test.md
```

### 1.1 Create Local SQS Queues

For each service that consumes from SQS, create isolated queues:

```bash
aws sqs create-queue --queue-name akshay-local-<service>-<purpose> --profile ai-dev --region ap-south-1
```

**CRITICAL**: Never consume from staging queues — this steals messages from the staging environment.

### 1.2 Resolve Staging Secrets

Read CDK config to find SSM parameter paths, then resolve:

```bash
cat ~/myequal-ai-cdk/equalai/config/test/equalai-test-config.ts
aws ssm get-parameter --name "/myequal/<short-name>/test/<param>" --with-decryption --profile ai-dev --region ap-south-1 --query 'Parameter.Value' --output text
```

### 1.3 Generate .env.staging-local Per Service

Each service gets its own `.env.staging-local`:

```env
# Staging infrastructure
DATABASE_URL=<from-ssm>
REDIS_URL=<from-ssm>
AWS_DEFAULT_REGION=ap-south-1

# Local SQS queues (NEVER staging queues)
CALL_EVENTS_QUEUE_URL=<local-queue-url>
PROCESSING_QUEUE_URL=<local-queue-url>

# Cross-service discovery (localhost)
USER_SERVICES_URL=http://localhost:8000
AI_BACKEND_URL=http://localhost:8001
MEMORY_SERVICE_URL=http://localhost:8002

# Disable tracing locally
DD_TRACE_ENABLED=false

# Test flags
TEST_SKIP_OTP_ENABLED=true
```

### 1.4 Start Services

```bash
for each service in matrix:
  cd ~/<repo>
  git checkout <branch>
  cp .env .env.backup.original 2>/dev/null || true
  cp .env.staging-local .env
  nohup uv run uvicorn app.main:app --host 0.0.0.0 --port <port> > /tmp/<service>-local.log 2>&1 &
  # Wait for health
  for i in {1..30}; do
    curl -s http://localhost:<port>/health > /dev/null 2>&1 && break
    sleep 1
  done
```

### 1.5 Verify Cross-Service Connectivity

```bash
for port in 8000 8001 8002; do
  curl -sS http://localhost:${port}/health 2>/dev/null && echo "port ${port}: healthy" || echo "port ${port}: not running"
done
```

If a service fails to start, report immediately and skip scenarios that depend on it.

---

## Step 2: Execute E2E Scenarios

For each E2E scenario from the design doc, execute in order.

### Scenario Execution Protocol

For each `E2E-N` scenario:

1. **Setup** — Create any prerequisite data (DB records, auth tokens, test users)
2. **Trigger** — Make the API call or send the SQS message that starts the flow
3. **Wait** — Allow async processing to complete (poll or sleep with timeout)
4. **Assert** — Verify all expected outcomes:

#### Assertion Types

**API Response Assertions:**
```bash
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST http://localhost:<port>/api/v2/<endpoint> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '<payload>')
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')
# Assert status code, response fields
```

**DB State Assertions:**
```bash
psql "<staging-db-url>" -c "
  SELECT id, status, updated_at
  FROM <table>
  WHERE <condition>
  ORDER BY updated_at DESC
  LIMIT 5;
"
```

**SQS Event Assertions:**
```bash
# Check if expected message was published to a queue
aws sqs receive-message --queue-url <local-queue-url> \
  --max-number-of-messages 1 --wait-time-seconds 10 \
  --profile ai-dev --region ap-south-1
# Verify message body matches expected schema
```

**Log Assertions:**
```bash
# Verify expected log entries
grep "<expected-pattern>" /tmp/<service>-local.log
# Verify NO error logs
grep -c "ERROR" /tmp/<service>-local.log
```

**S3 Assertions:**
```bash
aws s3 ls s3://<bucket>/<prefix>/ --profile ai-dev --region ap-south-1
```

**Cross-Service Flow Assertions:**
```bash
# Trace a correlation ID across services
CORRELATION_ID="<id>"
grep "$CORRELATION_ID" /tmp/user-services-local.log
grep "$CORRELATION_ID" /tmp/ai-backend-local.log
```

### Scenario Result Format

Per scenario, record:
```
E2E-N: <description>
  Trigger: POST /api/v2/<endpoint> → 201
  Assert API response: PASS (status 201, body contains expected fields)
  Assert DB state: PASS (record created with status=active)
  Assert SQS event: PASS (message published to queue)
  Assert logs: PASS (no errors, expected flow logged)
  VERDICT: PASS
```

---

## Step 3: Validate Logs & Staging

### 3.1 Local Log Validation

After all scenarios complete:

```bash
for svc in user-services ai-backend memory-service; do
  echo "=== $svc ==="
  ERROR_COUNT=$(grep -c "ERROR" /tmp/$svc-local.log 2>/dev/null || echo "0")
  WARN_COUNT=$(grep -c "WARNING" /tmp/$svc-local.log 2>/dev/null || echo "0")
  echo "Errors: $ERROR_COUNT | Warnings: $WARN_COUNT"
  if [ "$ERROR_COUNT" -gt 0 ]; then
    echo "--- Error samples ---"
    grep "ERROR" /tmp/$svc-local.log | tail -5
  fi
done
```

### 3.2 Datadog Staging Validation

Check Datadog for error spikes in staging during the test window:

```bash
# Query DD logs for errors in the test time window
curl -s -X POST "https://api.datadoghq.com/api/v2/logs/events/search" \
  -H "DD-API-KEY: ${CLAUDE_DD_API_KEY}" \
  -H "DD-APPLICATION-KEY: ${CLAUDE_DD_APP_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {
      "query": "service:(<service>) status:error env:test",
      "from": "<test-start-time>",
      "to": "now"
    },
    "page": {"limit": 10}
  }'
```

---

## Step 4: Handle Failures

If any scenario fails:

### 4.1 Diagnose Root Cause

1. Check local service logs for the failing service
2. Check DB state — was the prerequisite data correct?
3. Check SQS — was the message format correct?
4. Identify which **repo/service** caused the failure

### 4.2 Fix Cycle (if called from /ship-implement)

When invoked from `/ship-implement`, report failures back to the parent:

```
FAILED SCENARIOS:
- E2E-2: Expected status 201, got 500. Root cause: user-services missing field validation.
  Fix: user-services/app/api/v2/<endpoint>.py — add validation for <field>
```

The parent skill routes the fix to the appropriate coder agent.

**Max fix cycles**: 3 (configurable via `--max-fix-cycles`)

### 4.3 Fix Cycle (if invoked standalone)

When invoked directly via `/ship-test <ticket>`:

1. Diagnose the failure
2. **Ask the user** before making code changes: "E2E-2 failed because X. Fix in <file>?"
3. If approved, make the fix and re-run the failing scenario
4. Max 3 cycles, then report remaining failures

---

## Step 5: Cleanup

**CRITICAL**: Always clean up, even if tests fail.

```bash
# 1. Kill all local services
pkill -f "uvicorn app.main:app.*--port 8000" 2>/dev/null
pkill -f "uvicorn app.main:app.*--port 8001" 2>/dev/null
pkill -f "uvicorn app.main:app.*--port 8002" 2>/dev/null

# 2. Delete local SQS queues
for queue_url in <each-local-queue-url>; do
  aws sqs delete-queue --queue-url "$queue_url" --profile ai-dev --region ap-south-1
done

# 3. Restore .env files
for repo in myequal-ai-user-services myequal-ai-backend myequal-ai-memory-service; do
  cd ~/$repo 2>/dev/null || continue
  [ -f .env.backup.original ] && mv .env.backup.original .env
  rm -f .env.staging-local
done

# 4. Clean up test data in staging DB (if any was created)
psql "<staging-db-url>" -c "DELETE FROM <table> WHERE <test-data-condition>;"

# 5. Clean up test S3 objects (if any)
aws s3 rm s3://<bucket>/<test-prefix>/ --recursive --profile ai-dev --region ap-south-1
```

Skip cleanup if `--skip-cleanup` was passed (for debugging).

---

## Step 6: Report

```markdown
# E2E Test Report — <TICKET-ID>

## Design Doc: <Confluence URL>
## Services Tested: <list with branches>

## Scenario Results

| # | Scenario | API | DB | SQS | Logs | Verdict |
|---|----------|-----|----|-----|------|---------|
| E2E-1 | <description> | PASS | PASS | PASS | PASS | PASS |
| E2E-2 | <description> | PASS | FAIL | N/A | PASS | FAIL |
| E2E-3 | <description> | PASS | PASS | PASS | PASS | PASS |

## Log Validation

| Service | Errors | Warnings | Status |
|---------|--------|----------|--------|
| user-services | 0 | 2 | CLEAN |
| ai-backend | 1 | 0 | WARN — <error summary> |

## Datadog Staging: PASS / WARN

## Fix Cycles: N/3 used

## Overall Verdict: PASS / FAIL

### Failures (if any)
- E2E-2: <root cause>, <fix applied or recommended>

### Evidence
<DB query results, log excerpts, API responses for failed scenarios>
```

---

## Guard Rails

- **NEVER use production databases or queues** — staging only (`--profile ai-dev`)
- **NEVER consume from staging SQS queues** — always create local isolated queues
- **Always clean up** — kill services, delete queues, restore .env files
- **Max 3 fix cycles** — don't loop forever on a failing test
- **Design doc is the source of truth** — test what the design specifies, not what you assume
- **If no design doc found**, generate scenarios from branch changes but warn the user
- **If a service won't start**, skip it and mark dependent scenarios as SKIP (not FAIL)
- **Time-bound**: Total test execution should not exceed 15 minutes. If approaching limit, skip remaining scenarios and report partial results.

## Error Handling

| Error | Action |
|-------|--------|
| Design doc not found | Generate scenarios from branch diff. Warn user. |
| No feature branches found | Error — cannot test without code changes |
| Service fails to start | Report error, skip dependent scenarios, still run independent ones |
| DB connection refused | Check VPN. If down, abort with clear message. |
| SQS queue creation fails | Check AWS permissions. Try with `--profile ai-dev`. |
| Staging secrets missing from SSM | Check CDK config for correct parameter paths |
| Port already in use | `lsof -i :<port>`, kill the process, retry |
| Redis timeout on first connect | Wait 30s, retry. Normal for cold connect. |
