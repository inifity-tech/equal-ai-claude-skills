---
name: ship-design
description: Production-grounded design loop — discovers current state from prod logs/DB/traces, runs /design agents with that context, iterates until approved, publishes to Confluence.
disable-model-invocation: false
---

# /ship-design — Production-Grounded Design Until Approved

This skill produces a design document grounded in **actual production state** — not assumptions. It queries Datadog, the production DB, and codebase before running the design agents, then iterates until the design passes review.

## Parameters

`$ARGUMENTS` can include:
- Jira ticket ID (required, e.g., `EQ-1234`)
- Feature description in quotes (optional override)
- `--no-publish` flag to skip Confluence
- `--max-iterations N` to override iteration limit (default: 5)

Example: `/ship-design EQ-1234`, `/ship-design EQ-1234 "Add contact sync v2 with batch operations"`

---

## Step 1: Requirements Gathering

### 1.1 Fetch Jira Ticket

```
Tool: mcp__claude_ai_Atlassian__getJiraIssue
→ Extract: summary, description, acceptance criteria, labels, components, linked issues
```

### 1.2 Fetch Linked Confluence Pages

```
Tool: mcp__claude_ai_Atlassian__searchConfluenceUsingCql
→ Query: text matching ticket ID or feature name
→ For each result: mcp__claude_ai_Atlassian__getConfluencePage
→ Extract: requirements, context, constraints, acceptance criteria
```

### 1.3 Build Requirements Summary

```markdown
## Requirements Summary
- **Ticket**: EQ-1234
- **Feature**: <name>
- **Acceptance Criteria**: <list>
- **Scope**: <in-scope / out-of-scope>
- **Constraints**: <any constraints from ticket or linked docs>
```

**Guard rail**: If requirements are too sparse (just a title, no description or acceptance criteria), proceed with what's available and note assumptions in the design document. Do NOT block on user clarification.

---

## Step 2: Production Discovery

**CRITICAL**: Before designing anything, understand the current system state. Read the full reference at `~/.claude/skills/ship-design/references/production-discovery.md`.

### 2a. Code & Architecture Discovery

1. Identify affected repos based on requirements
2. Read existing HLD/LLD docs for those services:
   ```
   Read ~/myequal-ai-<repo>/docs/architecture/HLD.md
   Read ~/myequal-ai-<repo>/docs/architecture/LLD-*.md
   ```
3. Trace current code paths with Grep/Read across repos
4. Map current inter-service dependencies (HTTP clients, SNS publishers, SQS consumers)

### 2b. Production Logs Analysis

Query Datadog for the last 7 days on affected flows:

```
mcp__dd__get_logs(query="service:<service> <flow_keyword>", from="-7d", to="now", limit=100)
mcp__dd__list_traces(query="service:<service> resource_name:<endpoint>", from="-7d", to="now", limit=50)
```

Extract: request volume, error rates, latency baselines, actual service-to-service call patterns.

### 2c. Database State Analysis

Query production read replica (`$PPS_PROD_DB_URL` env var, **read-only**, `--profile ai-prod-ro` for any AWS access):

```sql
-- Row counts for affected tables
SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE relname IN ('<tables>');

-- Schema inspection
\d+ <tablename>

-- Data distribution
SELECT <grouping_col>, COUNT(*) FROM <table> GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- Growth patterns
SELECT date_trunc('day', created_at) AS day, COUNT(*) FROM <table>
WHERE created_at > NOW() - INTERVAL '30 days' GROUP BY 1 ORDER BY 1;
```

### 2d. Cross-Repo Dependency Mapping

For each affected service, identify actual dependencies:
- HTTP calls (grep for client code)
- Event publishing (grep for SNS/SQS code)
- Event consuming (grep for consumer code)
- Shared resources (DynamoDB tables, S3 buckets, Redis keys)

Cross-check code dependencies against Datadog trace data to verify what actually happens in production.

### 2e. Output: Current State Summary

Produce a structured document:

```markdown
## Current State Summary

### Architecture (from code + traces)
<Service interaction descriptions grounded in code and traces>

### Production Baselines
| Metric | Value | Source |
|--------|-------|--------|

### Data Volumes
| Table | Rows | Daily Growth | Size |
|-------|------|-------------|------|

### Dependency Map
<Mermaid diagram of actual service interactions>

### Known Issues
<Any existing issues discovered during analysis>
```

---

## Step 3: Design Generation

Run `/design` logic with the Current State Summary as additional context.

Spawn **4 parallel sub-agents** (Agent tool, NOT TeamCreate — these are independent, no inter-agent communication needed):

1. **HLD Agent** — reads `~/.claude/agents/hld.md`, receives requirements + current state summary
2. **LLD Agent** — reads `~/.claude/agents/lld.md`, receives requirements + current state summary
3. **NFR Agent** — reads `~/.claude/agents/nfr.md`, receives requirements + current state summary (already has baselines!)
4. **LLM Engineering Agent** — reads `~/.claude/agents/llm-engineering.md`, receives requirements + current state summary

Each agent receives the **Current State Summary** from Step 2 as additional context. This grounds their designs in reality.

After all 4 return, synthesize into a full design document (same format as `/design` skill — Executive Summary, Cross-Perspective Alignment, full agent outputs, Implementation Roadmap).

---

## Step 4: Publish to Confluence

```
Tool: mcp__claude_ai_Atlassian__createConfluencePage
- spaceId: "924188674"
- parentPageId: "924188772"
- title: "Design: <Feature Name> — <TICKET-ID>"
- body: <full_synthesized_document>
```

Link to Jira:
```
Tool: mcp__claude_ai_Atlassian__addCommentToJiraIssue
- issueIdOrKey: <ticket_id>
- body: "Architecture design document: <confluence_url>\nGenerated by /ship-design with production discovery."
```

---

## Step 5: Automated Design Review Loop

**MANDATORY — DO NOT SKIP THIS STEP.** You MUST run the review loop after synthesizing the design. Do NOT auto-approve. Do NOT proceed to Step 6 without at least one review pass returning zero actionable comments. Skipping this step was a real bug in a prior run — the review never ran and design issues leaked into implementation.

Use the `/review` skill to review the design, then iterate until no comments remain. This replaces manual approval — the exit criteria is a **clean review pass** (zero actionable comments).

### 5.1 Run Review

Invoke the `/review` skill against the design artifacts (the in-repo HLD, LLD, and NFR files produced in Step 3):

```
Tool: Skill(skill: "review", args: "<TICKET-ID> --design-review --files docs/hld/<hld-file>.md docs/lld/<lld-file>.md docs/nfr/<nfr-file>.md")
```

The review skill will evaluate:
1. Does the design address ALL requirements from the Jira ticket and Confluence page?
2. Are diagrams consistent with the text?
3. Are per-repo implementation tasks clear and actionable?
4. Are there must-fix or should-fix issues?
5. Is the design grounded in the current state (not making assumptions)?
6. Are API contracts complete (request/response schemas, error codes)?
7. Are NFR targets realistic given production baselines?

The review outputs a list of **comments** (actionable findings). Each comment specifies which document/section needs fixing.

### 5.2 Fix-and-Re-review Loop

```
iteration = 0
max_iterations = 5

while iteration < max_iterations:
    review_result = run_review()

    if review_result.comments is empty:
        → APPROVED (exit loop, proceed to Step 6)

    for each comment in review_result.comments:
        → Identify which agent produced the affected section (HLD, LLD, NFR, or LLM Eng)
        → Re-run ONLY the affected agent(s) with the specific fix instructions
        → Update the in-repo doc files with revised sections
        → Update the Confluence page with revised content

    # MANDATORY Post-Fix Re-Review: After ALL fix agents have updated their
    # sections, the loop MUST iterate back to run_review() above. Do NOT
    # assume the fixes resolved everything — only a clean review pass
    # (zero comments) counts as APPROVED. This re-review catches cases
    # where a fix introduces new issues or doesn't fully address the comment.
    iteration += 1

if iteration == max_iterations and comments still remain:
    → Log remaining unresolved comments in progress file
    → Mark design_verdict as APPROVED_WITH_CAVEATS
    → Auto-proceed — do NOT ask the user. Remaining issues will be noted in the final report.
```

### Key Rules for the Review Loop:
- **MANDATORY Post-Fix Re-Review** — after fix agents update their sections, you MUST re-run the `/review` skill to verify the fixes. Do NOT mark the design as APPROVED based on the fix agent claiming it addressed the comment. Only the reviewer's verdict counts. This was a real bug in a prior run where fixes were applied but never re-reviewed.
- **Re-run only affected agents** — if the review flags an NFR issue, only re-run the NFR agent with the fix instructions, not all 4
- **Update Confluence after each fix iteration** — the Confluence page should always reflect the latest state
- **Track iteration count** in progress file (`iteration: N/5`)
- **Exit criteria**: Zero actionable comments from the review skill = clean pass = APPROVED

---

## Step 6: Output — DESIGN_STATE

Update the progress file at `~/.claude/ship-state/<ticket-id>/progress.md`:

```markdown
## Loop 1: Design
- status: completed
- iteration: <N>/5
- confluence_url: <url>
- confluence_page_id: <page_id>
- affected_repos: [<repos>]
- design_verdict: APPROVED
- blockers: []
```

Present to user:
1. **Confluence URL**
2. **Executive Summary** (3-5 bullets)
3. **Affected Repos** with high-level changes per repo
4. **Implementation Roadmap** (phased)

This output becomes the input for `/ship-implement`.
