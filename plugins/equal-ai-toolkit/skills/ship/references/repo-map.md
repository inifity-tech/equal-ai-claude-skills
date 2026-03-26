# Repository Map

Central reference for all MyEqual AI repositories, their locations, service names, ports, and Datadog identifiers.

## Repositories

| Repo | Local Path | Service Name | Short Name | Local Port | Datadog Service | Staging Host |
|------|-----------|--------------|------------|------------|-----------------|--------------|
| myequal-ai-user-services | ~/myequal-ai-user-services | user-services | user-services | 8000 | user-services | user.ai-test.equal.in |
| myequal-ai-backend | ~/myequal-ai-backend | ai-backend | ai-backend | 8001 | ai-backend | backend-2.ai-test.equal.in |
| myequal-ai-memory-service | ~/myequal-ai-memory-service | memory-service | memory-service | 8002 | memory-service | memory.ai-test.equal.in |
| myequal-ai-cdk | ~/myequal-ai-cdk | — | cdk | — | — | — |
| myequal-ai-lambdas | ~/myequal-ai-lambdas | — | lambdas | — | — | — |

## Production Hosts

| Service | Production Host | DD Service |
|---------|----------------|------------|
| user-services | user.ai-prod.equal.in | user-services |
| ai-backend | backend-2.ai-prod.equal.in | ai-backend |
| memory-service | memory.ai-prod.equal.in | memory-service |
| api-gateway | business-api.equal.in | api-gateway |

## Port Allocation for Multi-Service Local Testing

When running multiple services locally for cross-service E2E tests:

| Service | Port | Health Check |
|---------|------|-------------|
| user-services | 8000 | http://localhost:8000/health |
| ai-backend | 8001 | http://localhost:8001/health |
| memory-service | 8002 | http://localhost:8002/health |

## SQS Queue Naming Convention

Local queues for testing use the pattern: `akshay-local-<short-name>-<purpose>`

Examples:
- `akshay-local-us-call-events` (user-services call events)
- `akshay-local-ab-processing` (ai-backend processing)
- `akshay-local-ms-memory-updates` (memory-service updates)

## CDK Config Paths

| Environment | Config File |
|-------------|------------|
| test/staging | ~/myequal-ai-cdk/equalai/config/test/equalai-test-config.ts |
| preprod | ~/myequal-ai-cdk/equalai/config/preprod/equalai-preprod-config.ts |
| prod | ~/myequal-ai-cdk/equalai/config/prod/equalai-prod-config.ts |

## SSM Parameter Prefix

All SSM parameters follow: `/myequal/<short-name>/<environment>/<param>`

Example: `/myequal/user-services/test/database-url`

## Git Branch Naming Convention

Feature branches: `feature/<ticket-id>-<short-description>`
Example: `feature/EQ-1234-contact-sync-v2`

## AWS Profile Usage

| Context | Profile |
|---------|---------|
| Development / staging | `ai-dev` (default) |
| Production read-only | `ai-prod-ro` |
| CDK deploy (test) | `ai-dev` |
| CDK deploy (prod) | Requires CI/CD pipeline |
