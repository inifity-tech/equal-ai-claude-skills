---
name: ship-consolidate
description: Cross-repo consolidation — validates resource consistency, event schemas, API contracts, PII handling across all PRs, runs final integration test, maps requirements coverage, and produces deployment plan.
disable-model-invocation: false
---

# /ship-consolidate — Cross-Repo Validation + Integration Safety

This skill is the final safety gate before shipping. It verifies that changes across multiple repos are consistent, runs a final integration test, maps every acceptance criterion to a PR, and produces a deployment plan.

## Parameters

`$ARGUMENTS` can include:
- Jira ticket ID (required, e.g., `EQ-1234`)
- `--prs 783,14,55` — PR numbers (comma-separated, reads repos from progress file)
- `--skip-integration-test` — skip the final integration test
- `--skip-requirements-check` — skip requirements traceability

Example: `/ship-consolidate EQ-1234`, `/ship-consolidate EQ-1234 --prs 783,14,55`

---

## Step 1: Load Context

### 1.1 Read Progress File

```
Read ~/.claude/ship-state/<ticket-id>/progress.md
→ Extract: all PR numbers, branches, repos, confluence_url
```

### 1.2 Read Requirements

```
Tool: mcp__claude_ai_Atlassian__getJiraIssue
→ Extract: acceptance criteria
```

### 1.3 Read Design Document

```
Tool: mcp__claude_ai_Atlassian__getConfluencePage
→ Extract: expected resource names, event schemas, API contracts
```

---

## Step 2: Cross-Repo Consistency (Consolidator Agent)

Spawn the consolidator agent (foreground, sequential — its output feeds into the next step):

```
Agent(
  description: "Cross-repo consolidation",
  subagent_type: "consolidator",
  prompt: """
  Run cross-repo validation for ticket <TICKET-ID>.

  PRs to analyze:
  <list of repo + PR number pairs>

  Repo paths:
  <from ~/.claude/skills/ship/references/repo-map.md>

  Design document context:
  <key sections: resource names, event schemas, API contracts from design>

  Produce the full consolidation report as specified in your methodology.
  """
)
```

### Handle Consolidation Results

- **All PASS**: Proceed to Step 3
- **Any FAIL**:
  1. Route failures back to the relevant repo coder agents with specific fix instructions
  2. After fixes are applied and pushed, **MANDATORY Post-Fix Re-Consolidation**: You MUST re-run the consolidator agent (Step 2) to verify that ALL checks now PASS. Do NOT assume the fixes resolved everything — only a clean consolidator pass counts. This was a real bug in a prior run where cross-repo field name mismatches were fixed but never re-validated, risking silent feature failure.
  3. Repeat until consolidator returns all PASS or bounce-back limit reached
  4. Update progress file with blockers
  5. **Max 2 bounce-backs to implementation** — after that, escalate to user
- **Any WARN**: Log warnings, proceed (present to user in final report)

---

## Step 3: Full Integration Test

Run the **complete multi-service E2E test** as a final safety net. This is the same as the `/ship-implement` E2E test, but runs AFTER all review fixes and consolidation fixes.

Read the multi-service testing reference at `~/.claude/skills/ship-implement/references/multi-service-test.md`.

### 3.1 Spawn Tester Agent

```
Agent(
  description: "Final integration test",
  subagent_type: "general-purpose",
  prompt: """
  Run FINAL integration test for ticket <TICKET-ID>.

  IMPORTANT: This is the final safety gate. Be thorough.

  Read: ~/.claude/skills/ship-implement/references/multi-service-test.md

  SERVICE MATRIX:
  <all affected services on their feature branches>

  TEST SCENARIOS — include cross-service flows:
  1. <End-to-end user flow: API call → processing → storage → response>
  2. <Event flow: publish in service A → consume in service B → verify state>
  3. <Error handling: simulate failure conditions, verify graceful degradation>
  4. <Data consistency: verify data format consistency across service boundaries>

  Additional checks from consolidation report:
  <any WARN items from consolidation that should be verified at runtime>

  PROTOCOL:
  1. Create local SQS queues
  2. Start all affected services locally (feature branches)
  3. Execute ALL test scenarios
  4. Validate local logs across ALL services
  5. Query Datadog staging
  6. Cleanup everything

  OUTPUT:
  - Per-scenario PASS/FAIL with evidence
  - Cross-service flow validation: PASS/FAIL
  - Log validation: PASS/WARN/FAIL
  - Overall verdict: PASS/FAIL
  """
)
```

### 3.2 Handle Test Failures

- **PASS**: Proceed to Step 4
- **FAIL**: Route back to `/ship-implement` with diagnosis
  - Count as a bounce-back (max 2 total across consolidation + integration test)
  - After 2 bounce-backs: escalate to user with full failure details

---

## Step 4: Requirements Traceability

Map each Jira acceptance criterion to specific PR changes:

### 4.1 Extract Acceptance Criteria

From the Jira ticket, list each acceptance criterion:
```
AC1: Users can sync contacts in batch
AC2: Sync status is visible in the UI
AC3: Failed syncs are retried automatically
...
```

### 4.2 Map to PR Changes

For each acceptance criterion, identify which PR(s) and which specific changes satisfy it:

```markdown
| # | Acceptance Criterion | PR(s) | Files/Changes | Status |
|---|---------------------|-------|---------------|--------|
| AC1 | Batch contact sync | user-services #783 | app/api/v2/contacts.py, app/services/contact_sync.py | COVERED |
| AC2 | Sync status in UI | user-services #783 | app/api/v2/sync_status.py | COVERED |
| AC3 | Auto-retry failed syncs | lambdas #14 | functions/retry_sync/handler.py | COVERED |
| AC4 | Rate limiting on sync API | — | — | GAP |
```

### 4.3 Flag Gaps

If any acceptance criterion is NOT covered by any PR:
- **Mark as GAP**
- Determine if it's a scope issue (intentionally deferred) or a miss
- Present to user for decision

---

## Step 5: Deployment Planning

Based on the PRs and their dependencies, produce a deployment plan:

### 5.1 Deployment Order

Determine the correct order based on dependencies:

```markdown
## Deployment Order

1. **CDK** (PR #55) — infrastructure must exist first
   - DynamoDB tables, SQS queues, SSM parameters
   - Deploy to test → verify → deploy to prod

2. **Database Migrations** (if any)
   - Run Alembic migrations on staging DB first
   - Verify backward compatibility

3. **Services** (PRs #783)
   - user-services: deploy to test → verify health + logs → deploy to prod
   - ai-backend: no changes (or deploy if modified)

4. **Lambdas** (PR #14)
   - Deploy after CDK (needs SQS event source)
   - Verify with test event
```

### 5.2 Merge Order

May differ from deployment order:

```markdown
## Merge Order

1. CDK PR #55 → merge first (triggers CDK pipeline)
2. Wait for CDK deploy to complete in test
3. user-services PR #783 → merge (triggers ECS deploy)
4. lambdas PR #14 → merge (triggers Lambda deploy)
```

### 5.3 Pre-Deployment Checklist

```markdown
## Pre-Deployment Checklist

- [ ] SSM parameters created for new config values
- [ ] Feature flag created in Statsig (if applicable)
- [ ] CDK diff reviewed (no unexpected changes)
- [ ] Database migration tested on staging
- [ ] Monitoring dashboards/alerts configured
- [ ] Rollback plan documented
```

---

## Step 6: Output — CONSOLIDATION_STATE

### 6.1 Update Progress File

```markdown
## Loop 3: Consolidation
- status: completed
- cross_repo_check: pass
- integration_test: pass
- requirements_coverage: full (or gaps_found)
- deployment_order: [cdk, user-services, lambdas]
- merge_order: [cdk, user-services, lambdas]
- final_verdict: SHIP
```

### 6.2 Present Final Report

```markdown
=== CONSOLIDATION REPORT ===
Ticket: EQ-1234
Feature: <feature name>
Design: <confluence_url>

## Cross-Repo Validation
| Category | Pass | Fail | Warn |
|----------|------|------|------|
| Resource Names | 5 | 0 | 0 |
| Event Schemas | 3 | 0 | 0 |
| API Contracts | 2 | 0 | 0 |
| Data Formats | 4 | 0 | 0 |
| PII Handling | 6 | 0 | 0 |

## Integration Test
Verdict: PASS
Scenarios: 4/4 passed

## Requirements Coverage
| AC | Description | Status |
|----|-------------|--------|
| AC1 | ... | COVERED |
| AC2 | ... | COVERED |

Gaps: None

## Deployment Plan
1. CDK PR #55 → merge & deploy
2. user-services PR #783 → merge & deploy
3. lambdas PR #14 → merge & deploy

## Pre-Deployment
- [ ] SSM parameters: ready
- [ ] Feature flag: created
- [ ] Rollback plan: documented

## FINAL VERDICT: SHIP
```

---

## Error Handling

| Failure | Response |
|---------|----------|
| Consolidation finds FAIL | Route back to /ship-implement (max 2 bounce-backs) |
| Integration test fails | Diagnose, route back to /ship-implement (counts toward bounce-back limit) |
| Requirements gap found | Present to user — is it intentionally deferred or a miss? |
| Bounce-back limit reached (2) | Escalate to user with all remaining issues |
| Progress file missing | Ask user for PR numbers and design URL |

## Important Rules

- **Consolidator runs FIRST** — don't run integration tests until cross-repo consistency is verified
- **Integration test is mandatory** — unless explicitly skipped with `--skip-integration-test`
- **Every acceptance criterion must be mapped** — gaps must be acknowledged
- **Deployment order matters** — infrastructure before application code
- **Update progress file at every transition** — enables cross-session resume
