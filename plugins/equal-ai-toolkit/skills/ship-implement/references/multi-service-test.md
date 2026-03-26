# Multi-Service Local Testing Reference

How to run multiple MyEqual AI services locally for cross-service E2E testing.

## Concept

Each service runs locally on its assigned port, pointing at staging infrastructure (DB, Redis, S3) but using **dedicated local SQS queues** for isolation. Services discover each other via localhost URLs.

## Port Assignments

| Service | Port | Repo Path |
|---------|------|-----------|
| user-services | 8000 | ~/myequal-ai-user-services |
| ai-backend | 8001 | ~/myequal-ai-backend |
| memory-service | 8002 | ~/myequal-ai-memory-service |

## Setup Protocol

### Step 1: Determine Which Services Need Local Instances

Only services with code changes need to run on feature branches. Unchanged services either:
- Run on master (if needed for the test flow)
- Don't run at all (if not part of the test flow)

Example:
```
Feature touches: user-services, lambdas, cdk
Test flow requires: user-services → ai-backend (for processing)

Run locally:
- user-services: feature/EQ-1234-... branch (has changes)
- ai-backend: master branch (needed for flow, no changes)
- memory-service: skip (not in test flow)
- lambdas: tested separately (Lambda functions, not a local server)
- cdk: no runtime (infrastructure only, verify via resource names)
```

### Step 2: Create Local SQS Queues

For each service that consumes from SQS, create isolated local queues:

```bash
# User-services queues
aws sqs create-queue --queue-name akshay-local-us-<purpose> --profile ai-dev --region ap-south-1

# AI-backend queues
aws sqs create-queue --queue-name akshay-local-ab-<purpose> --profile ai-dev --region ap-south-1
```

**CRITICAL**: Never consume from staging queues locally — this steals messages from the staging environment.

### Step 3: Resolve Staging Secrets

For each service, resolve its secrets from AWS SSM:

```bash
# Read CDK config first to find SSM parameter paths
cat ~/myequal-ai-cdk/equalai/config/test/equalai-test-config.ts

# Then resolve each parameter
aws ssm get-parameter --name "/myequal/<short-name>/test/<param>" \
  --with-decryption --profile ai-dev --region ap-south-1 \
  --query 'Parameter.Value' --output text
```

### Step 4: Generate .env.staging-local Per Service

Each service gets its own `.env.staging-local` with:

```env
# === Staging infrastructure ===
DATABASE_URL=<from-ssm>
REDIS_URL=<from-ssm>
AWS_DEFAULT_REGION=ap-south-1

# === Local SQS queues (NEVER staging queues) ===
CALL_EVENTS_QUEUE_URL=<local-queue-url>
PROCESSING_QUEUE_URL=<local-queue-url>

# === Cross-service discovery (localhost) ===
USER_SERVICES_URL=http://localhost:8000
AI_BACKEND_URL=http://localhost:8001
MEMORY_SERVICE_URL=http://localhost:8002

# === Disable tracing locally ===
DD_TRACE_ENABLED=false

# === Test flags ===
TEST_SKIP_OTP_ENABLED=true
```

**Key difference from single-service testing**: The cross-service discovery URLs point to localhost ports instead of staging hosts.

### Step 5: Start Services

Start each service in sequence (or parallel if independent):

```bash
# For each service:
cd ~/myequal-ai-<repo>
git checkout <branch>  # feature branch or master

# Backup and swap env
cp .env .env.backup.original 2>/dev/null || true
cp .env.staging-local .env

# Start
nohup uv run uvicorn app.main:app --host 0.0.0.0 --port <port> > /tmp/<short-name>-local.log 2>&1 &
```

Wait for each to be healthy before starting dependent services:
```bash
sleep 30
curl -sS http://localhost:<port>/health
```

### Step 6: Verify Cross-Service Connectivity

After all services are up, verify they can talk to each other:

```bash
# Check user-services can reach ai-backend
curl -sS http://localhost:8000/health  # should show dependencies healthy

# Check ai-backend can reach user-services
curl -sS http://localhost:8001/health
```

## Test Execution

### API Tests
```bash
# Trigger flow in service A
curl -X POST http://localhost:8000/api/v2/<endpoint> \
  -H "Content-Type: application/json" \
  -d '{"key": "value"}'

# Verify result in service B
curl http://localhost:8001/api/v2/<verification-endpoint>
```

### Event Flow Tests
```bash
# Send SQS message to service A's local queue
aws sqs send-message \
  --queue-url <local-queue-url> \
  --message-body '<CloudEvents JSON payload>' \
  --profile ai-dev --region ap-south-1

# Wait for processing
sleep 5

# Verify in DB
psql "<staging-db-url>" -c "SELECT ... WHERE ...;"

# Verify in service B's logs
grep "<correlation-id>" /tmp/<service-b>-local.log
```

### DB Assertions
```bash
psql "<staging-db-url>" -c "
  SELECT id, status, updated_at
  FROM <table>
  WHERE <condition>
  ORDER BY updated_at DESC
  LIMIT 5;
"
```

### S3 Assertions
```bash
aws s3 ls s3://<bucket>/<prefix>/ --profile ai-dev --region ap-south-1
```

## Log Validation

Check each service's local logs:
```bash
# Errors across all services
for svc in user-services ai-backend memory-service; do
  echo "=== $svc ==="
  grep -c "ERROR" /tmp/$svc-local.log 2>/dev/null || echo "not running"
done

# Trace a flow across services
CORRELATION_ID="<id>"
grep "$CORRELATION_ID" /tmp/user-services-local.log
grep "$CORRELATION_ID" /tmp/ai-backend-local.log
```

## Cleanup

**CRITICAL**: Always clean up after testing.

```bash
# 1. Kill all local services
pkill -f "uvicorn app.main:app.*--port 8000" 2>/dev/null
pkill -f "uvicorn app.main:app.*--port 8001" 2>/dev/null
pkill -f "uvicorn app.main:app.*--port 8002" 2>/dev/null

# 2. Delete local SQS queues
aws sqs delete-queue --queue-url <each-local-queue-url> --profile ai-dev --region ap-south-1

# 3. Restore .env files
for repo in myequal-ai-user-services myequal-ai-backend myequal-ai-memory-service; do
  cd ~/$repo
  [ -f .env.backup.original ] && mv .env.backup.original .env
  rm -f .env.staging-local
done

# 4. Clean up test data in staging DB
psql "<staging-db-url>" -c "DELETE FROM <table> WHERE <test-data-condition>;"

# 5. Clean up test S3 objects
aws s3 rm s3://<bucket>/<test-prefix>/ --recursive --profile ai-dev --region ap-south-1
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Service won't start | Check `.env` for missing vars. Compare with CDK config. |
| Port already in use | `lsof -i :<port>` to find and kill the process |
| Service can't reach another | Verify the other service is healthy. Check env var for URL. |
| SQS messages not consumed | Verify queue URL in `.env` matches the created queue. Check consumer logs. |
| DB connection refused | Verify VPN is connected. Check DB URL from SSM. |
| Redis timeout | Normal on first connect (~15-30s). Wait and retry. |
