---
name: ship-implement
description: Implementation loop — decomposes design into per-repo tasks, creates agent teams for parallel implementation, runs review cycles and E2E tests until all PRs are approved.
disable-model-invocation: false
---

# /ship-implement — Implement, Review, E2E Test Until Approved

This skill takes a design document and implements it across multiple repos using agent teams. Each repo gets a coder agent working in worktree isolation. A shared reviewer validates each PR. A shared tester runs real E2E tests.

## Parameters

`$ARGUMENTS` can include:
- Jira ticket ID (required, e.g., `EQ-1234`)
- `--design-url <confluence_url>` — Confluence page with the design doc
- `--design-page-id <id>` — Confluence page ID (alternative to URL)
- `--repos repo1,repo2` — override affected repos (default: read from progress file or design doc)
- `--skip-review` — skip review loop (for rapid iteration)
- `--skip-test` — skip E2E testing (for incremental work)

Example: `/ship-implement EQ-1234`, `/ship-implement EQ-1234 --design-url https://...`

---

## Step 1: Load Design Context

### 1.1 Read Design Document

If `--design-url` or `--design-page-id` provided:
```
Tool: mcp__claude_ai_Atlassian__getConfluencePage
→ Extract: Implementation Roadmap, per-repo tasks, API contracts, DB changes, event schemas
```

If neither provided, check progress file:
```
Read ~/.claude/ship-state/<ticket-id>/progress.md
→ Extract: confluence_url from Loop 1
```

### 1.2 Extract Per-Repo Task Lists

From the design doc's **Implementation Roadmap** and **LLD: Code Architecture** sections, extract:

```markdown
## Repo: user-services
Tasks:
1. Add Alembic migration for new table
2. Create SQLModel + manager
3. Add API endpoint POST /api/v2/...
4. Add SNS publisher for new event
5. Update settings.py with new config

## Repo: myequal-ai-cdk
Tasks:
1. Add DynamoDB table definition
2. Add SQS queue + DLQ
3. Add SSM parameters
4. Update ECS task definition env vars

## Repo: lambdas
Tasks:
1. Add new Lambda function
2. Add SQS event source mapping
3. Add DynamoDB write logic
```

### 1.3 Create Jira Subtasks for Implementation Tracking

For each logical component identified in the per-repo task lists, create a Jira subtask under the parent ticket. Group at the **logical component level** — not per-file, not per-repo.

Typical subtask breakdown for a backend feature:

| Subtask | Scope |
|---------|-------|
| DB Schema + Models | Migration, SQLModel models, enums |
| External Client | API client, settings, auth |
| Data Layer | DB managers (sync + async) |
| Service Logic | Business logic orchestrator, event publishing |
| API Endpoints | Router, endpoints, request/response models |
| Infrastructure (CDK) | SSM params, DynamoDB tables, SQS queues |

For each subtask:
```
Tool: mcp__claude_ai_Atlassian__createJiraIssue
- cloudId: "fe4a2218-e425-4b39-ba1c-ab0ba14be9a4"
- projectKey: "EAI"
- issueTypeName: "Subtask"
- parent: "<TICKET-ID>"
- summary: "<TICKET-ID>: <component name>"
- description: "<task list for this component from the design doc>"
```

Store the subtask keys in the progress file under `Loop 2 > subtasks`.

**Subtask lifecycle during implementation:**
- Created → **To Do** (at decomposition)
- Coder agent starts working on it → **In Progress** (transition via `mcp__claude_ai_Atlassian__transitionJiraIssue`)
- Coder agent commits and pushes → **Done** (transition after code is on the branch)
- If review requests changes → back to **In Progress**

### 1.4 Identify Dependencies Between Repos

Mark tasks that depend on other repos:
- CDK resources must exist before app code references them
- Event publisher schema must match consumer expectations
- API contracts must align between caller and server

---

## Step 2: Create Implementation Team

Use TeamCreate to create a coordinated team:

```
Tool: TeamCreate
- team_name: "ship-<ticket-id>"
```

### Spawn Teammates

**Per repo** — `coder-<repo>` agents:
- Each is a general-purpose agent with **worktree isolation**
- Each receives: design doc context + its repo-specific task list
- Each works independently on a feature branch

**Shared** — `reviewer` agent:
- Runs `/review` on each PR after coder completes
- Posts review findings back to the team

**Shared** — `tester` agent:
- Runs E2E tests following `/ship-test` skill protocol
- Only activated after ALL repos have PRs ready

---

## Step 3: Parallel Implementation

For each repo, spawn a coder agent (use Agent tool with `isolation: "worktree"`):

```
Agent(
  description: "Implement <repo> changes",
  subagent_type: "general-purpose",
  isolation: "worktree",
  prompt: """
  You are implementing changes for ticket <TICKET-ID> in <repo>.

  FIRST: Read the full design document from Confluence:
  Tool: mcp__claude_ai_Atlassian__getConfluencePage(pageId: "<page_id>")

  REPO: ~/myequal-ai-<repo>
  BRANCH: feature/<ticket-id>-<short-description>

  YOUR TASKS:
  <per-repo task list from Step 1.2>

  IMPLEMENTATION RULES:
  1. Follow existing code patterns in the repo (manager pattern, async/await, etc.)
  2. Read existing similar code before writing new code
  3. Run any existing tests: `uv run pytest` (fix if broken)
  4. Create a feature branch and commit your changes
  5. Create a PR with `gh pr create` — include ticket ID in title
  6. Do NOT add unnecessary comments, docstrings, or type annotations to unchanged code
  7. Keep changes minimal and focused on the task

  GIT RULES:
  - Branch from master: `git checkout -b feature/<ticket-id>-<desc> master`
  - Never commit to master directly
  - PR title format: "<TICKET-ID>: <short description>"

  JIRA SUBTASK TRACKING:
  As you complete each logical component, transition its Jira subtask:
  - When starting a component: transition subtask to "In Progress"
  - When component code is committed: transition subtask to "Done"
  - Use: mcp__claude_ai_Atlassian__transitionJiraIssue(issueIdOrKey, transitionId)
  - First call mcp__claude_ai_Atlassian__getTransitionsForJiraIssue to get available transition IDs

  Subtask assignments:
  <map of subtask key → component name from Step 1.3>

  OUTPUT:
  - Branch name
  - PR number and URL
  - List of files changed with subtask mapping (which files belong to which subtask)
  - Any blockers or cross-repo dependencies you couldn't resolve
  """
)
```

**Launch all coder agents in parallel** — they work in separate worktrees and don't depend on each other for implementation (cross-repo consistency is checked later in consolidation).

---

## Step 4: Review Loop (Per Repo)

After each coder agent completes and creates a PR:

### 4.1 Run Review

For each PR, spawn a review agent:

```
Agent(
  description: "Review <repo> PR",
  prompt: """
  Run /review on PR #<number> in ~/myequal-ai-<repo>.

  Focus areas for this review:
  - Does the implementation match the design doc?
  - Are existing patterns followed?
  - Are there security issues (PII in logs, SQL injection, etc.)?
  - Are there performance issues (N+1 queries, missing indexes)?

  Design doc context: <key sections from design>
  """
)
```

### 4.2 Handle Review Feedback

If reviewer returns `REQUEST_CHANGES`:
1. Extract specific fix instructions from the review
2. Spawn coder agent again with fix instructions:
   ```
   Agent(
     description: "Fix <repo> review feedback",
     prompt: "Fix the following review comments on PR #<number> in ~/myequal-ai-<repo>:\n<review_comments>\n\nPush fixes to the existing branch."
   )
   ```
3. **MANDATORY Post-Fix Re-Review**: After the coder agent pushes fixes, you MUST re-run the reviewer agent (Step 4.1) on the same PR. Do NOT mark the repo as APPROVED based on the coder agent claiming it fixed everything — only the reviewer's APPROVE verdict counts. This was a real bug in a prior run where fixes were applied but never re-reviewed, allowing issues to leak through.
4. Repeat steps 1-3 until reviewer returns `APPROVE` or iteration limit reached
5. **Max 5 iterations per repo**

If reviewer returns `APPROVE`:
- Mark repo as review-approved in progress file
- Move to testing once ALL repos are approved

---

## Step 5: E2E Testing

**Only run after ALL repos have reviewed PRs.**

Read the multi-service testing reference at `~/.claude/skills/ship-implement/references/multi-service-test.md`.

### 5.1 Determine Test Setup

Based on affected repos, determine the service startup matrix:

```
| Service | Port | Branch | Local SQS Queues |
|---------|------|--------|-----------------|
| user-services | 8000 | feature/EQ-1234-... | akshay-local-us-* |
| ai-backend | 8001 | master (no changes) | akshay-local-ab-* |
```

### 5.2 Spawn Tester Agent

```
Agent(
  description: "E2E test for <ticket>",
  subagent_type: "general-purpose",
  prompt: """
  Run E2E tests for ticket <TICKET-ID> following the /ship-test skill protocol.

  FIRST: Read the multi-service test reference:
  Read ~/.claude/skills/ship-implement/references/multi-service-test.md

  SERVICE MATRIX:
  <service startup matrix from 5.1>

  DESIGN CONTEXT:
  <key API contracts, event schemas, expected flows from design doc>

  TEST SCENARIOS:
  1. <Primary happy path — trigger in service A, verify in service B>
  2. <Error path — invalid input, verify graceful handling>
  3. <Event flow — publish event, verify consumer processes it>
  4. <DB assertions — verify expected state after flow>

  PROTOCOL:
  1. Create local SQS queues
  2. Generate .env.staging-local for each service (with cross-service localhost URLs)
  3. Start each service locally
  4. Execute test scenarios
  5. Validate local logs for errors
  6. Query Datadog staging for error spikes
  7. Cleanup: kill services, delete queues, restore .env files

  OUTPUT:
  - Per-scenario PASS/FAIL with evidence
  - Local log validation: PASS/WARN/FAIL
  - Datadog staging validation: PASS/WARN/FAIL
  - Overall verdict: PASS/FAIL
  """
)
```

### 5.3 Handle Test Failures

If tests fail:
1. Diagnose which service/repo caused the failure
2. Route back to that repo's coder agent with fix instructions
3. Re-run tests after fix
4. **Max 3 test-fix cycles**

---

## Step 6: Completion

### 6.1 Update Progress File

```markdown
## Loop 2: Implementation
- status: completed
- repos:
  - user-services: { branch: feature/EQ-1234-..., pr: #783, review: APPROVED, test: PASS }
  - cdk: { branch: feature/EQ-1234-..., pr: #55, review: APPROVED, test: PASS }
  - lambdas: { branch: feature/EQ-1234-..., pr: #14, review: APPROVED, test: PASS }
- subtasks:
  - EAI-1390: { component: "DB Schema + Models", status: Done, files: ["migrations/versions/xxx.py", "app/models/xxx.py"] }
  - EAI-1391: { component: "External Client", status: Done, files: ["app/clients/xxx.py"] }
  - EAI-1392: { component: "Service Logic", status: Done, files: ["app/services/xxx.py"] }
  - EAI-1393: { component: "API Endpoints", status: Done, files: ["app/api/v2/xxx.py"] }
  - EAI-1394: { component: "Infrastructure (CDK)", status: Done, files: ["lib/xxx-stack.ts"] }
- blockers: []
```

### 6.2 Present to User

```
=== IMPLEMENTATION COMPLETE ===
Ticket: EQ-1234
Design: <confluence_url>

PRs:
| Repo | PR | Branch | Review | Test |
|------|-----|--------|--------|------|
| user-services | #783 | feature/EQ-1234-... | APPROVED | PASS |
| cdk | #55 | feature/EQ-1234-... | APPROVED | PASS |
| lambdas | #14 | feature/EQ-1234-... | APPROVED | PASS |

E2E Test: PASS
Ready for consolidation.
```

---

## Error Handling

| Failure | Response |
|---------|----------|
| Design doc not found | Ask user for Confluence URL or page ID |
| Coder agent fails | Report partial progress, log the failure, continue with other repos |
| Review loop exhausted (5 iterations) | Proceed with remaining issues logged in progress file |
| Test setup fails (service won't start) | Check logs, try without that service, continue pipeline |
| Cross-service test failure | Diagnose root cause service, route to that coder |
| Test loop exhausted (3 cycles) | Proceed with failures logged in progress file |
| Git conflict on feature branch | Rebase on master, retry. If rebase fails, force-create a clean branch. |

## Important Rules

- **Worktree isolation for coders** — each coder works in an isolated git worktree
- **Never commit to master** — always feature branches
- **Real E2E tests** — not unit tests. Services run locally against staging infra.
- **Local SQS queues only** — never consume from staging queues
- **Clean up after testing** — kill services, delete queues, restore .env
- **Update progress file at every state transition** — enables cross-session resume
