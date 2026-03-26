---
name: ship
description: End-to-end feature delivery — from Jira ticket to production-ready PRs. Orchestrates design (production-grounded), implementation (multi-repo, agent teams), and consolidation (cross-repo validation, integration tests, deployment planning).
disable-model-invocation: false
---

# /ship — End-to-End Feature Delivery Pipeline

Meta-orchestrator that drives a feature from Jira ticket to production-ready PRs across multiple repos. Runs three loops fully autonomously — no manual approval gates. The user is only notified at the end when everything is complete.

```
Loop 1: Design (production-grounded) →
Loop 2: Implement (multi-repo, review, E2E test, Jira subtask tracking) →
Loop 3: Consolidate (cross-repo validation, integration test, deploy plan) →
Loop 4: Phased PRs (split for human review — presentation only, code already validated) →
Done: Jira comment with all links → Notify user
```

## Parameters

`$ARGUMENTS` must include:
- Jira ticket ID (required, e.g., `EQ-1234`)

Optional flags:
- `--skip-design` — skip Loop 1 (use existing design)
- `--design-url <url>` — use existing Confluence design page
- `--resume` — resume from last progress state (auto-detected if progress file exists)
- `--repos repo1,repo2` — override affected repos

Example: `/ship EQ-1234`, `/ship EQ-1234 --skip-design --design-url https://...`, `/ship EQ-1234 --resume`

---

## References

Read these reference files as needed during orchestration:
- `~/.claude/skills/ship/references/progress-schema.md` — progress file format and resume protocol
- `~/.claude/skills/ship/references/repo-map.md` — repo paths, ports, service names

---

## Step 0: Session Lock + Resume Check

### 0.0 Acquire Session Lock

Before any work, check for concurrent sessions on the same ticket:

```bash
cat ~/.claude/ship-state/<ticket-id>/session.lock 2>/dev/null
```

If lock file exists:
1. Read the `pid` from the lock
2. Check if that process is alive: `kill -0 <pid> 2>/dev/null`
3. If **alive**: another session is working on this ticket
   - Tell user: "Session `<session_id>` (PID `<pid>`) is currently working on `<ticket>` since `<started>` (loop: `<loop>`). Force take over, or abort?"
   - **Force**: delete lock, create new one, proceed
   - **Abort**: exit immediately
4. If **dead**: stale lock from a crashed session — delete it, proceed

Create (or replace) the lock:
```bash
mkdir -p ~/.claude/ship-state/<ticket-id>
echo "session_id: $(uuidgen)\nstarted: $(date -u +%Y-%m-%dT%H:%M:%S)\npid: $$\nloop: init" > ~/.claude/ship-state/<ticket-id>/session.lock
```

**Release the lock** on pipeline completion. This allows another session to resume if this one crashes.

### 0.1 Check for Existing Progress

```bash
ls ~/.claude/ship-state/<ticket-id>/progress.md 2>/dev/null
```

If progress file exists:
1. Read `~/.claude/ship-state/<ticket-id>/progress.md`
2. Parse `current_loop` and `status` from frontmatter
3. Present current status summary:

```
=== EXISTING PROGRESS FOUND ===
Ticket: EQ-1234
Feature: <feature name>
Started: 2026-03-10
Current Loop: implement
Status: in_progress

Loop 1 (Design): COMPLETED — <confluence_url>
Loop 2 (Implementation):
  - user-services: PR #783 (review: APPROVED, test: PASS)
  - lambdas: PR #14 (review: iteration_2, test: not_started)
Loop 3 (Consolidation): not_started
```

4. **Auto-resume**: jump to `current_loop` and continue automatically. Do NOT ask the user — always resume from where we left off.

**Backward compatibility**: If the progress file shows `status: completed` but has NO `Loop 4: Phased PRs` section (or it shows `status: not_started`), the pipeline was completed before Loop 4 was added. In this case:
   - Add the Loop 4 section to the progress file
   - Set `current_loop: phased_prs`, `status: in_progress`
   - Resume from Loop 4 (Step 6)

### 0.2 Initialize Progress File (New or Fresh Start)

```bash
mkdir -p ~/.claude/ship-state/<ticket-id>
```

Write initial progress file:
```markdown
---
ticket: <TICKET-ID>
feature: <to be filled after Jira fetch>
started: <today's date>
current_loop: design
status: in_progress
---

## Loop 1: Design
- status: not_started
- iteration: 0/5
- confluence_url:
- confluence_page_id:
- affected_repos: []
- design_verdict: NOT_STARTED
- blockers: []

## Loop 2: Implementation
- status: not_started
- repos: []
- blockers: []

## Loop 3: Consolidation
- status: not_started
- cross_repo_check: pending
- integration_test: pending
- requirements_coverage: pending
- deployment_order: []
- merge_order: []
- final_verdict: NOT_STARTED

## Loop 4: Phased PRs
- status: not_started
- orchestrator_pr:
- phase_prs: []
```

---

## Step 1: Validate Jira Ticket

```
Tool: mcp__claude_ai_Atlassian__getJiraIssue(issueIdOrKey: "<TICKET-ID>")
```

Extract and store:
- Summary → update progress file `feature` field
- Description, acceptance criteria
- Linked issues, components, labels

If ticket not found: ask user to verify the ticket ID.

---

## Step 2: Check for Existing Design

If `--skip-design` or `--design-url` provided, skip to Step 4.

Otherwise, check if a design already exists:
```
Tool: mcp__claude_ai_Atlassian__searchConfluenceUsingCql
→ Query: title contains "Design" AND text contains "<TICKET-ID>"
```

If found, **always create a new design** — do not reuse existing designs. Ignore the existing one and proceed to Step 3.

---

## Step 3: Loop 1 — Design

Invoke `/ship-design`:

```
Tool: Skill(skill: "ship-design", args: "<TICKET-ID>")
```

This will:
1. Gather requirements from Jira + Confluence
2. Run production discovery (Datadog, DB, code)
3. Spawn 4 design agents in parallel
4. Iterate design review up to 5 times
5. Publish to Confluence
6. Update progress file

**Wait for completion.** The skill updates the progress file with Loop 1 state.

---

## GATE 1: Design Auto-Approval (Review Loop)

The `/ship-design` skill now includes an automated review loop (using `/review`). It iterates until the review returns zero actionable comments, then exits with `design_verdict: APPROVED`.

Read the updated progress file after `/ship-design` completes:

```bash
cat ~/.claude/ship-state/<ticket-id>/progress.md
```

### If design_verdict is APPROVED:

Present a summary to the user (informational, not a gate):

```
=== DESIGN COMPLETE (auto-approved via review loop) ===
Design: <confluence_url>
Review iterations: <N>/5
Verdict: APPROVED (clean review pass)

Executive Summary:
<3-5 bullets from design>

Affected Repos:
<list with high-level changes per repo>

Implementation Roadmap:
<phased roadmap summary>
```

Update progress → `current_loop: implement`, proceed to Step 4 automatically.

### If design_verdict is APPROVED_WITH_CAVEATS:

The review loop hit max iterations with remaining comments. Log the caveats but auto-proceed — do NOT ask the user.

Update progress → `current_loop: implement`, proceed to Step 4 automatically. The remaining issues will be noted in the final report.

---

## Step 4: Loop 2 — Implementation

Invoke `/ship-implement`:

```
Tool: Skill(skill: "ship-implement", args: "<TICKET-ID>")
```

This will:
1. Read design from Confluence
2. Decompose into per-repo tasks
3. Create agent team for parallel implementation
4. Review each PR (up to 5 iterations per repo)
5. Run E2E tests (multi-service if needed)
6. Update progress file

**Wait for completion.**

---

## GATE 2: Implementation Auto-Proceed

After `/ship-implement` completes, read the updated progress file. If all PRs have `review: APPROVED` and `test: PASS`, auto-proceed to consolidation. Do NOT ask the user for approval.

Update progress → `current_loop: consolidate`, proceed to Step 5 automatically.

---

## Step 5: Loop 3 — Consolidation

Invoke `/ship-consolidate`:

```
Tool: Skill(skill: "ship-consolidate", args: "<TICKET-ID>")
```

This will:
1. Run cross-repo consistency checks (consolidator agent)
2. Run final integration test
3. Map requirements to PRs
4. Produce deployment plan
5. Update progress file

**Wait for completion.**

---

## GATE 3: Ship Auto-Proceed

After `/ship-consolidate` completes, read the updated progress file. If `cross_repo_check: pass`, `integration_test: pass`, and `requirements_coverage: full`, auto-proceed to phased PRs. Do NOT ask the user for approval.

Update progress → `current_loop: phased_prs`, proceed to Step 6 automatically.

---

## Step 6: Loop 4 — Phased PRs (Review-Ready Split)

**Purpose**: The code is already implemented, reviewed by agents, tested end-to-end, and consolidated. This loop splits the validated code into smaller, reviewable PRs for human reviewers. It is purely a presentation layer — no new code is written.

**When to skip**: If a repo's PR is under 500 lines, skip phased splitting for that repo (it's already reviewable).

### 6.1 Determine Which Repos Need Splitting

For each repo in the progress file:
1. Check PR size: `gh pr diff <number> --stat | tail -1` to get total lines changed
2. If **≤500 lines**: skip — the PR is already reviewable
3. If **>500 lines**: split into phases

### 6.2 Split Large PRs

For each repo that needs splitting, invoke `/raise-phased-prs` in autonomous mode:

```
Tool: Skill(skill: "raise-phased-prs", args: "<TICKET-ID> --repo <repo> --autonomous --design-url <confluence_url>")
```

This will:
1. Analyze the feature branch commits and changed files
2. Group changes into logical phases (schema → models/managers → clients → service logic → API + wiring)
3. Create phase branches that stack on each other
4. Create phase PRs (each targeting the previous phase branch)
5. Convert the original PR into the orchestrator PR (or create a new one targeting master with the full diff)

**Phase grouping for typical backend features:**

| Phase | Contents | Target |
|-------|----------|--------|
| 1 — Schema | Migration + SQLModel models + enums | master |
| 2 — Data Layer | DB managers (sync + async) | phase-1 branch |
| 3 — Client | External API clients + settings | phase-2 branch |
| 4 — Service | Business logic + event publishers | phase-3 branch |
| 5 — API + Wiring | Endpoints + router + env.py | phase-4 branch |

**Rules:**
- Each phase PR should be ≤500 lines (soft limit — can exceed if splitting would break logical cohesion)
- Each phase must be independently reviewable (reviewer can understand it without reading other phases)
- No phase should break existing functionality if merged alone (additive-only until the final wiring phase)
- The orchestrator PR shows the full diff and links to all phase PRs

### 6.3 Update Progress File

```markdown
## Loop 4: Phased PRs
- status: completed
- orchestrator_pr: <url>
- phase_prs:
  - user-services: [phase-1-schema: #801, phase-2-data-layer: #802, phase-3-client: #803, phase-4-service: #804, phase-5-api: #805]
  - cdk: [] (under 500 lines, not split)
```

### 6.4 Update Jira Subtasks

For each phase PR created, add a comment to the corresponding Jira subtask with the phase PR link. This connects the implementation subtasks to the reviewable PRs.

---

## Step 7: Finalize

### 7.1 Jira Comment

```
Tool: mcp__claude_ai_Atlassian__addCommentToJiraIssue
- issueIdOrKey: <TICKET-ID>
- body: """
Feature implementation complete. Ready for deployment.

**Design**: <confluence_url>

**PRs (Orchestrator)**:
- user-services: <orchestrator_pr_url>
- cdk: <pr_url>

**Phased Review PRs** (if split):
- Phase 1 — Schema: <phase_1_pr_url>
- Phase 2 — Data Layer: <phase_2_pr_url>
- Phase 3 — Client: <phase_3_pr_url>
- Phase 4 — Service: <phase_4_pr_url>
- Phase 5 — API + Wiring: <phase_5_pr_url>

**Merge Order**: CDK → user-services (orchestrator PR)
**Deployment Order**: CDK → user-services

**Validation**:
- Cross-repo consistency: PASS
- Integration test: PASS
- Requirements coverage: FULL

**Review Guide**: Review phase PRs in order (1→5) for comprehension. Approve and merge the orchestrator PR for deployment.

Generated by `/ship` pipeline.
"""
```

### 7.2 Release Lock & Archive Progress

```bash
rm -f ~/.claude/ship-state/<ticket-id>/session.lock
```

Update progress file:
```
status: completed
final_verdict: SHIP
```

### 7.3 Final Message

```
=== SHIP COMPLETE ===
Ticket: EQ-1234 — <feature name>
Design: <confluence_url>

Orchestrator PRs: <list with URLs>
Phased Review PRs: <list with URLs, or "N/A — PRs under 500 lines">
Jira: Updated with summary + subtasks

Merge order: CDK → user-services (merge orchestrator PR, not phase PRs)
Review guide: Review phase PRs in order (1→5), then approve orchestrator PR.
Next step: Review phased PRs, merge orchestrator PRs in order, monitor deployments.
```

---

## Error Handling

| Failure | Response |
|---------|----------|
| Jira ticket not found | Stop pipeline, notify user with error |
| Design loop exhausted (5 iterations) | Proceed with APPROVED_WITH_CAVEATS, log remaining issues |
| Implementation loop exhausted | Proceed with remaining issues logged, note in final report |
| Consolidation bounce-back limit (2) | Proceed with remaining issues logged, note in final report |
| Session ends mid-pipeline | Progress file persists; next session resumes via Step 0 |
| Confluence API failure | Fall back to terminal output, continue pipeline |
| GitHub API failure | Fall back to terminal output for PR details |
| Agent timeout | Report partial progress, write to progress file, continue with next step |

## Important Rules

- **Fully autonomous** — the entire pipeline runs without user intervention. Do NOT ask for approval at any gate. Only notify the user when the pipeline is complete (or on unrecoverable failure).
- **Always create a new design** — never reuse existing designs found in Confluence
- **Always auto-resume** — if progress file exists, resume from where we left off without asking
- **Always update progress file at state transitions** — this enables cross-session resume
- **Never commit to master** — all changes go through PRs on feature branches
- **Production read-only** — use `ai-prod-ro` profile for any production AWS access
- **Clean up after testing** — kill services, delete SQS queues, restore .env files
- **Confluence space**: Always publish to Equal AI space (ID: 924188674)
