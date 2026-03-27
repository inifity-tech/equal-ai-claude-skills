---
name: review
description: "Architecture review — spawns 4 parallel agents (HLD, LLD, NFR, LLM Engineering) to review code changes, runs LSP static analysis for type errors and missing fields, validates against live environments (Datadog logs, DB state) for evidence-backed findings, then synthesizes and posts to GitHub"
disable-model-invocation: false
---

# Architecture Review Skill

This skill runs a **parallel multi-perspective architecture review** against code changes (PR, branch, or files). Four specialist agents review simultaneously, then results are synthesized into a single report.

## Parameters

`$ARGUMENTS` can include:
- PR number (e.g., `#123` or `123`)
- Branch name (e.g., `feature/my-branch`)
- File path(s) (e.g., `app/services/call_session.py`)
- `--no-post` flag to skip GitHub comment and only output to terminal

Example: `/review 123`, `/review feature/my-branch`, `/review app/api/v2/`

---

## Step 1: Determine Review Scope

Based on `$ARGUMENTS`, gather the diff and changed files:

### If PR number provided:
```bash
gh pr diff <number>
gh pr view <number> --json files,title,body,baseRefName
```

### If branch name provided:
```bash
git diff master...<branch>
git diff master...<branch> --name-only
```

### If file path(s) provided:
Read the file(s) directly. Use `git diff HEAD -- <path>` if there are uncommitted changes, otherwise read the full file for a design review.

### If nothing provided:
Use the current branch:
```bash
git diff master...HEAD
git diff master...HEAD --name-only
```

**Guard rail**: If no changes are detected (empty diff and no files), inform the user and stop. Do NOT spawn agents.

## Step 2: Categorize Affected Areas

Analyze the changed files to determine which areas are affected. This helps agents focus:

| Pattern | Area |
|---------|------|
| `app/api/` | API endpoints |
| `app/models/` | Database models |
| `app/db_*_manager.py` | Database access layer |
| `app/services/` | Business logic |
| `app/event_publishers/`, `app/event_consumers/` | Event system |
| `app/redis_stream_processors/` | Real-time streaming |
| `app/clients/` | External integrations |
| `app/middleware/` | Middleware pipeline |
| `app/settings.py` | Configuration |
| `migrations/` | Database migrations |
| `tests/` | Test changes |
| `prompts/`, `*prompt*` | LLM/prompt changes |

Build a summary string like: `"Areas affected: API endpoints (v2/conversations), Database models (CallLog), Event publishers (call_completed)"`

## Step 3: Spawn 4 Review Agents IN PARALLEL

**CRITICAL: You MUST spawn all 4 agents in a SINGLE message using 4 Task tool calls.** This ensures true parallel execution.

Store the diff content and changed file list in variables, then pass them to each agent. If the diff is very large (>10,000 lines), pass only the `--name-only` file list and instruct agents to read files themselves.

### Agent 1: HLD Review

```
Task(
  description: "HLD architecture review",
  subagent_type: "general-purpose",
  prompt: """
You are the HLD (High-Level Design) review agent. Your focus: component topology, data flow, cross-service impact, and architectural patterns.

FIRST: Read your full methodology from /Users/akshay/.claude/agents/hld.md

REVIEW CONTEXT:
- Changed files: <file_list>
- Areas affected: <areas_summary>
- Diff:
<diff_content>

YOUR TASK — Review these changes from an HLD perspective:
1. Read the relevant architecture docs (HLD.md for affected services)
2. Assess: Do these changes align with established component topology?
3. Check: Are data flow patterns consistent with existing architecture?
4. Check: Any cross-service impact or coupling concerns?
5. Check: Are established design patterns (manager pattern, event-driven, etc.) followed?
6. Check: Any new components introduced — are they justified and well-placed?

CLASSIFYING FINDINGS — PR-introduced vs Pre-existing:
For EVERY finding, determine whether it was INTRODUCED by this PR or is a PRE-EXISTING issue in surrounding code that the PR merely exposes or interacts with. Use the diff to make this determination:
- If the problematic code is in the diff's added lines (+ lines) → **PR-introduced**
- If the problematic code exists in unchanged surrounding code that the PR calls/uses → **Pre-existing**
- If the PR copies/follows a flawed existing pattern → **PR-introduced** (the PR author chose to replicate it), but note the pre-existing pattern

Tag each finding with `[NEW]` (introduced by this PR) or `[PRE-EXISTING]` (existed before this PR).

OUTPUT FORMAT — Use these EXACT headers. Be extremely concise — max 10 action items total. No prose, no theory.
## HLD Review

### Verdict: APPROVE | REQUEST_CHANGES
### Key Finding: <one-sentence summary>

### Action Items
Only 🔴 (must fix) and 🟡 (should fix). One line per item, max 15 words:
- 🔴/🟡 [NEW/PRE-EXISTING] **<short title>** — `file:line` — <what to fix>

If no action items: "No issues found."

IMPORTANT: Skip trivial or nice-to-have items entirely. Only flag things that would break production or violate architecture. Max 5 items.
"""
)
```

### Agent 2: LLD Review

```
Task(
  description: "LLD implementation review",
  subagent_type: "general-purpose",
  prompt: """
You are the LLD (Low-Level Design) review agent. Your focus: API contracts, database patterns, event schemas, code architecture, and implementation quality.

FIRST: Read your full methodology from /Users/akshay/.claude/agents/lld.md

REVIEW CONTEXT:
- Changed files: <file_list>
- Areas affected: <areas_summary>
- Diff:
<diff_content>

YOUR TASK — Review these changes from an LLD perspective:
1. Read the relevant LLD docs (LLD-api-layer, LLD-data-layer, LLD-event-system, etc.)
2. Assess: Do API contracts follow existing conventions (versioning, auth, response shapes)?
3. Check: Do database changes follow the manager pattern? Are migrations safe (IF EXISTS, downgrade tested)?
4. Check: Do event schemas follow CloudEvents format with self-skip guard?
5. Check: Are Redis key patterns consistent (naming, TTLs, data structures)?
6. Check: Is the code architecture consistent (imports at top, async/await, error handling)?
7. Check: Are there missing indexes, N+1 queries/writes, or connection pool concerns?
8. Check: Do DI dependency functions share a single DB session per request, or do they create multiple independent sessions?
9. Check: Are exception types used for error matching (not string comparisons like `"text" in str(e)`)? Custom exceptions should be defined and caught by type.
10. Check: Are operational parameters (retry counts, timeouts, delays, thresholds) pulled from settings/config, not hardcoded?
11. Check: When enums/constants are defined (e.g., `UserStatus.DELETED`), are all comparisons using the enum constant, not raw strings like `"deleted"`?
12. Check: For new migrations adding indexes, do existing indexes on the table already cover the query? Check `pg_indexes` before adding.
13. Check: Trace auth dependency chain (e.g., `Depends(get_current_user)`) into the endpoint — are there checks that duplicate what the dependency already enforces (dead code)?
14. Check: Are all FastAPI endpoints declared as `async def` (project convention)? Flag any `def` endpoints.
15. Check: Do all code paths return the declared return type? If an endpoint is typed `-> ModelResponse` but a branch returns `JSONResponse`, flag it — use `HTTPException` with `detail={...}` instead, which preserves the return type contract and uses FastAPI's standard error handling.
16. Check: Are error responses using `HTTPException` (not `JSONResponse`)? FastAPI endpoints should raise `HTTPException` for error cases (4xx/5xx), not return `JSONResponse` — this keeps the return type annotation clean and follows the framework's error handling conventions.

CLASSIFYING FINDINGS — PR-introduced vs Pre-existing:
For EVERY finding, determine whether it was INTRODUCED by this PR or is a PRE-EXISTING issue in surrounding code that the PR merely exposes or interacts with. Use the diff to make this determination:
- If the problematic code is in the diff's added lines (+ lines) → **PR-introduced**
- If the problematic code exists in unchanged surrounding code that the PR calls/uses → **Pre-existing**
- If the PR copies/follows a flawed existing pattern → **PR-introduced** (the PR author chose to replicate it), but note the pre-existing pattern

Tag each finding with `[NEW]` (introduced by this PR) or `[PRE-EXISTING]` (existed before this PR).

OUTPUT FORMAT — Use these EXACT headers. Be extremely concise — max 10 action items total. No prose, no theory.
## LLD Review

### Verdict: APPROVE | REQUEST_CHANGES
### Key Finding: <one-sentence summary>

### Action Items
Only 🔴 (must fix) and 🟡 (should fix). One line per item, max 15 words:
- 🔴/🟡 [NEW/PRE-EXISTING] **<short title>** — `file:line` — <what to fix>

If no action items: "No issues found."

IMPORTANT: Skip trivial or nice-to-have items entirely. Only flag real bugs, broken contracts, or unsafe patterns. Max 5 items.
"""
)
```

### Agent 3: NFR Review

```
Task(
  description: "NFR review",
  subagent_type: "general-purpose",
  prompt: """
You are the NFR (Non-Functional Requirements) review agent. Your focus: performance, scalability, reliability, observability, and security.

FIRST: Read your full methodology from /Users/akshay/.claude/agents/nfr.md

REVIEW CONTEXT:
- Changed files: <file_list>
- Areas affected: <areas_summary>
- Diff:
<diff_content>

YOUR TASK — Review these changes from an NFR perspective:
1. Read the relevant architecture docs (HLD for NFR sections, LLDs for implementation details)
2. PERFORMANCE: Any new DB queries without indexes? New external calls adding latency? Caching opportunities missed? N+1 writes (loop of `session.add()` that should be a single bulk UPDATE/DELETE)?
3. SCALABILITY: New unbounded data growth? Connection pool pressure? Fan-out amplification?
4. RELIABILITY: Missing error handling? No retry/timeout on external calls? Missing DLQ config? Single points of failure?
5. OBSERVABILITY: Missing Datadog traces/spans? Insufficient logging? Missing metrics? No monitors for new paths?
6. SECURITY: Auth gaps? SQL injection vectors? PII in logs? Missing input validation?

Optionally query Datadog (mcp__dd__list_traces, mcp__dd__get_logs) to check current baselines for affected endpoints.

CLASSIFYING FINDINGS — PR-introduced vs Pre-existing:
For EVERY finding, determine whether it was INTRODUCED by this PR or is a PRE-EXISTING issue in surrounding code that the PR merely exposes or interacts with. Use the diff to make this determination:
- If the problematic code is in the diff's added lines (+ lines) → **PR-introduced**
- If the problematic code exists in unchanged surrounding code that the PR calls/uses → **Pre-existing**
- If the PR copies/follows a flawed existing pattern → **PR-introduced** (the PR author chose to replicate it), but note the pre-existing pattern

Tag each finding with `[NEW]` (introduced by this PR) or `[PRE-EXISTING]` (existed before this PR).

OUTPUT FORMAT — Use these EXACT headers. Be extremely concise — max 10 action items total. No prose, no theory.
## NFR Review

### Verdict: APPROVE | REQUEST_CHANGES
### Key Finding: <one-sentence summary>

### Action Items
Only 🔴 (must fix) and 🟡 (should fix). One line per item, max 15 words. Quantify where possible:
- 🔴/🟡 [NEW/PRE-EXISTING] **<short title>** — `file:line` — <what to fix>

If no action items: "No issues found."

IMPORTANT: Skip trivial or nice-to-have items entirely. Only flag things that will cause outages, data loss, or resource leaks. Max 5 items.
"""
)
```

### Agent 4: LLM Engineering Review

```
Task(
  description: "LLM engineering review",
  subagent_type: "general-purpose",
  prompt: """
You are the LLM Engineering review agent. Your focus: prompt design, model selection, token efficiency, LLM observability, and LLM resilience patterns.

FIRST: Read your full methodology from /Users/akshay/.claude/agents/llm-engineering.md

REVIEW CONTEXT:
- Changed files: <file_list>
- Areas affected: <areas_summary>
- Diff:
<diff_content>

YOUR TASK — Determine if these changes involve LLM integrations:

IF the changes touch LLM-related code (prompts, model calls, PromptLayer, Gemini/Vertex/Ultravox, token tracking, LLM observability):
1. Read the relevant LLM architecture docs
2. Check: Is the prompt well-designed (clear instructions, structured output, few-shot examples)?
3. Check: Is the model selection appropriate for the task (Flash vs Pro, temperature, thinking budget)?
4. Check: Are token budgets estimated and reasonable?
5. Check: Is LLM observability instrumented (Datadog LLM Obs, token metrics)?
6. Check: Are resilience patterns in place (timeouts, retries, fallback parsing, provider fallbacks)?
7. Check: Is PromptLayer used for template versioning?

IF the changes do NOT touch LLM-related code:
- Check if the changes could INDIRECTLY affect LLM behavior (e.g., config changes, settings changes, data format changes that feed into LLM pipelines)
- If truly not applicable, output a brief "N/A" response

CLASSIFYING FINDINGS — PR-introduced vs Pre-existing:
For EVERY finding, determine whether it was INTRODUCED by this PR or is a PRE-EXISTING issue in surrounding code that the PR merely exposes or interacts with. Use the diff to make this determination:
- If the problematic code is in the diff's added lines (+ lines) → **PR-introduced**
- If the problematic code exists in unchanged surrounding code that the PR calls/uses → **Pre-existing**
- If the PR copies/follows a flawed existing pattern → **PR-introduced** (the PR author chose to replicate it), but note the pre-existing pattern

Tag each finding with `[NEW]` (introduced by this PR) or `[PRE-EXISTING]` (existed before this PR).

OUTPUT FORMAT — Use these EXACT headers. Be extremely concise — max 10 action items total. No prose, no theory.
## LLM Engineering Review

### Verdict: APPROVE | REQUEST_CHANGES | N/A
### Key Finding: <one-sentence summary, or "N/A">

### Action Items
Only 🔴 (must fix) and 🟡 (should fix). One line per item, max 15 words:
- 🔴/🟡 [NEW/PRE-EXISTING] **<short title>** — `file:line` — <what to fix>

If N/A or no action items: "N/A"

IMPORTANT: Self-select out quickly if not LLM-related. Skip nice-to-haves entirely. Max 5 items.
"""
)
```

## Step 4: Live Environment Validation

**While agents are running (or after they return)**, check if the changed code is deployed to any environment and validate against real logs and data. This step turns theoretical findings into evidence-backed findings.

### 4a: Detect Deployment

Identify key feature/provider/component names from the PR (e.g., a new provider name, a new endpoint path, a new event type). Then query Datadog across all environments:

```
mcp__dd__get_logs(query="<feature_keyword> service:<service_name> env:test", from=3_days_ago, to=now, limit=10)
mcp__dd__get_logs(query="<feature_keyword> service:<service_name> env:maxtest", from=3_days_ago, to=now, limit=10)
mcp__dd__get_logs(query="<feature_keyword> service:<service_name> env:preprod", from=3_days_ago, to=now, limit=10)
mcp__dd__get_logs(query="<feature_keyword> service:<service_name> env:production", from=3_days_ago, to=now, limit=10)
```

Build a deployment status table:
| Environment | Status | Evidence |
|---|---|---|
| Test | Yes/No | Session IDs or "No logs found" |
| Maxtest | Yes/No | Session IDs or "No logs found" |
| Preprod | Yes/No | Session IDs or "No logs found" |
| Production | Yes/No | Session IDs or "No logs found" |

**If NOT deployed anywhere**: Skip to Step 5 (synthesize agent results only).

**If deployed**: Continue with 4b-4d.

### 4b: Analyze Logs for Errors and Anomalies

For each environment where the code is deployed, query logs for the specific sessions/flows:

1. **Error/warning logs**: `mcp__dd__get_logs(query="<feature_keyword> service:<service> status:error OR status:warn")`
2. **Full session logs**: For each session ID found, query all logs for that session to reconstruct the lifecycle
3. **Look for**:
   - Runtime errors (exceptions, tracebacks, type errors)
   - Async/await bugs (coroutine objects not awaited, client-closed errors)
   - Resource leaks (connections not closed, background tasks erroring after cleanup)
   - Duplicate events (same structured event emitted multiple times for one session)
   - Missing events (session_start without session_end, or vice versa)
   - Unexpected state transitions
   - External service call failures (HTTP clients, WebSockets, APIs)

### 4c: Validate Database State

If the PR creates or modifies data records, check the database to verify correctness:

1. **Identify affected tables** from the code changes (look for model classes, DB writes, event payloads)
2. **Query the production read replica** (or appropriate env DB) for records created by the new flow:
   ```sql
   -- Example: Check if call records have expected fields populated
   SELECT session_id, recording_url, duration, call_status, created_at
   FROM calllog WHERE session_id IN ('<session_ids_from_logs>')
   ```
3. **Look for**:
   - NULL values in columns that should be populated
   - Missing records (flow completed in logs but no DB record)
   - Incorrect values (duration mismatch, wrong status)
   - Orphaned records (DB record without corresponding log events)

**Note**: Use the production read replica connection from the `PPS_PROD_DB_URL` environment variable. Only production data is available — preprod/test data requires separate DB access.

### 4d: Trace the Code Path for Each Bug

For each error found in logs:
1. **Read the exact source file and line** referenced in the error
2. **Identify the root cause** (e.g., sync function calling async method, missing await, wrong type)
3. **Determine blast radius** (does this affect all sessions or just edge cases?)
4. **Categorize**:
   - 🔴 **Critical**: Affects core functionality (e.g., recording broken, data not saved)
   - 🔴 **High**: Resource leaks or error storms on every session
   - 🟡 **Medium**: Duplicate logs, non-critical data missing
   - 🟢 **Low**: Cosmetic or logging-only issues

### 4e: Reconcile with Agent Findings

Compare live evidence against agent findings:
- **Upgrade**: If an agent flagged a theoretical issue and logs confirm it, upgrade to 🔴 with evidence
- **Downgrade**: If an agent flagged a concern but Statsig/config overrides it in practice, note this
- **Add NEW**: Bugs found only through live validation that no agent caught
- **Remove**: Drop findings that are provably not an issue based on live behavior

### 4f: LSP Static Analysis

Run LSP checks on the **changed files** to catch type errors, missing fields, unused parameters, and incorrect references that agents may miss. This step uses the LSP tool's language server to surface real compiler/type-checker diagnostics.

**When to run**: Always, for any PR that modifies Python files. This step can run in parallel with live validation (4b-4e).

**Procedure**:
1. Clone the repo and checkout the PR branch (if not already local)
2. For each changed `.py` file, run:
   - `LSP(operation="documentSymbol")` — get the structure of the file
   - `LSP(operation="hover")` — check types on key symbols (function params, return types, class fields)
   - Focus on: new classes, new function signatures, cross-file references, and Pydantic model instantiations
3. **What to look for** (only 🔴 and 🟡 — skip informational/style diagnostics):
   - 🔴 **Missing required fields**: Pydantic/dataclass models instantiated without required fields (will crash at runtime)
   - 🔴 **Type mismatches**: `Optional[X]` passed to non-optional `X`, wrong types at call sites
   - 🔴 **Undefined references**: Symbols that don't resolve (broken imports, renamed functions)
   - 🟡 **Unused parameters**: Function parameters accepted but never used (LSP `"not accessed"` diagnostic)
   - 🟡 **Inconsistent constant usage**: String literals used where named constants exist and are already imported
   - 🟡 **Resource lifecycle**: Clients/connections created but never closed
4. **Filter out noise**: Ignore `reportMissingImports` errors from unresolved virtualenv paths — these are environment artifacts, not real bugs
5. **Deduplicate against agent findings**: If an agent already flagged the same issue, don't add it again

**Output**: A flat list of items in the same format as agent findings:
```
- 🔴/🟡 [NEW] **<title>** — `file:line` — <what to fix> _(LSP)_
```

## Step 5: Synthesize Results

After ALL 4 agents return AND live validation is complete (if applicable), synthesize into a **concise, pinpoint report**. The entire comment MUST be **50 lines or fewer** (excluding the markdown table).

### Synthesis Rules:
1. **Build the summary table** from each agent's Verdict and Key Finding
2. **Determine Overall Verdict**:
   - If ANY agent flags a `[NEW]` (PR-introduced) 🔴 issue → Overall is `REQUEST_CHANGES`
   - If agents only flag `[PRE-EXISTING]` issues → Overall is `APPROVE` (pre-existing issues don't block the PR)
   - If all say `APPROVE` (or `N/A`) and no `[NEW]` issues → Overall is `APPROVE`
   - If live validation found 🔴 bugs in code introduced by this PR → Overall is `REQUEST_CHANGES`
3. **Merge all action items** from all agents, live validation, AND LSP analysis into a single flat list: 🔴 first, then 🟡
4. **NO 🟢 Nice to Have section** — drop all green/nice-to-have items entirely. They won't be acted on.
5. **Deduplicate aggressively** — if multiple agents flagged the same issue, merge into ONE item
6. **Classify PR-introduced vs Pre-existing** — For each item, verify whether the issue is in code added by this PR (`[NEW]`) or in pre-existing code that the PR interacts with (`[PRE-EXISTING]`). Cross-check against the diff: if the flagged line is NOT in the diff's `+` lines, it's pre-existing. Pre-existing issues should NOT block the PR — they are flagged as tech debt for backlog tracking.
7. **Evidence-backed items first** — items confirmed by live logs/DB appear before theoretical items
8. **Hard cap: max 10 items total** (🔴 + 🟡 combined). If agents produce more, keep only the highest-impact items.
9. **One line per item** — `file:line` + what to fix in ≤15 words. No multi-line explanations.
10. **Log evidence inline** — for live-validated bugs, append a short log excerpt on the same line (not a separate block quote)
11. **Test coverage check** — if the changed files include new endpoints, services, or manager methods but NO files under `tests/` were changed, add a 🟡 item: "No automated tests added for new endpoint/service/manager"

### Idempotency: Self-Review Before Posting

**CRITICAL: The review must be stable across re-runs on the same code.** Before posting:

1. **Check for existing review comments** on the PR:
   ```bash
   gh api repos/<owner>/<repo>/issues/<number>/comments --jq '.[].body' | grep -l "Architecture Review"
   ```
2. **If a previous review exists**:
   - Read the previous comment body
   - Compare the current diff (from Step 1) against the diff at the time of the previous review
   - **If the diff is unchanged**: Do NOT post a new comment. Tell the user: "Code unchanged since last review. No new comment needed."
   - **If the diff changed**: Post ONLY findings related to the NEW/CHANGED code. Do not re-raise items from the previous review that were already posted. Prefix the comment with: `> Updated review — new findings since previous review on <date>`
3. **Internal self-review iteration**: Before posting, re-read your synthesized report and ask:
   - Is every item backed by a specific file:line reference?
   - Is every item genuinely actionable (not just an observation)?
   - Would this item survive if I re-ran the review? If it's subjective or borderline, drop it.
   - Am I under 50 lines and 10 items?

### Report Format (max ~50 lines total):

```markdown
## Architecture Review — <APPROVE or REQUEST_CHANGES>

| Agent | Verdict | Finding |
|-------|---------|---------|
| HLD | ✅/❌ | <10 words> |
| LLD | ✅/❌ | <10 words> |
| NFR | ✅/❌ | <10 words> |
| LLM | ✅/❌/— | <10 words> |
| Live | ✅/❌/— | <deployed envs or "not deployed"> |
| LSP | ✅/❌ | <N issues found or "clean"> |

### 🔴 Must Fix
- **<title>** — `file:line` — <≤15 words> _(source)_ `evidence: "<log excerpt if any>"`

### 🟡 Should Fix
- **<title>** — `file:line` — <≤15 words> _(source)_

### 📋 Pre-existing Issues (tech debt, not blocking)
_Issues found in surrounding code not changed by this PR. Track in backlog._
- **<title>** — `file:line` — <≤15 words> _(source)_

_`/review` · HLD · LLD · NFR · LLM · Live · LSP_
```

**If all APPROVE with no items**:

```markdown
## Architecture Review — APPROVE

| Agent | Verdict | Finding |
|-------|---------|---------|
| HLD | ✅ | <finding> |
| LLD | ✅ | <finding> |
| NFR | ✅ | <finding> |
| LLM | ✅/— | <finding> |
| Live | ✅/— | <finding> |

No action items. Ship it.

_`/review` · HLD · LLD · NFR · LLM · Live · LSP_
```

**There is NO 🟢 Nice to Have section. Ever.**

## Step 6: Deliver Results

### If reviewing a PR (and `--no-post` not specified):

Post the synthesized report as a **single** GitHub PR comment:

```bash
gh pr comment <number> --body "$(cat <<'EOF'
<synthesized_report>
EOF
)"
```

The report should fit in one comment (~50 lines). If it somehow exceeds 65K chars, you have too many items — cut to top 10.

### If reviewing a branch or files (or `--no-post` specified):

Output the report directly to the terminal.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Agent fails/times out | Include an error note in that section: `"**Error**: HLD agent failed — <error message>. Review this perspective manually."` Continue with remaining agents. |
| GitHub comment fails | Warn user; display full report in terminal instead |
| Empty diff | Inform user: "No changes detected. Nothing to review." Do NOT spawn agents |
| Very large diff (>10K lines) | Pass file list only; instruct agents to read files themselves |
| PR not found | Error: "PR #<number> not found. Check the number and try again." |
| Datadog query fails | Skip live validation; note in report: "Live Env: N/A — Datadog query failed." Continue with agent results only. |
| No deployments found | Note in report: "Live Env: N/A — Not deployed to any environment." Skip steps 4b-4e. |
| DB query fails | Skip DB validation; note: "DB validation skipped — connection failed." Continue with log analysis. |
| Logs too large to parse | Use targeted queries (filter by error/warn status, specific session IDs) rather than broad queries. |

## Guard Rails

### Core
- **Read-only** — NEVER modifies code, creates commits, or pushes changes
- **Always spawn all 4 agents in parallel** — in a single message with 4 Task tool calls
- **Wait for ALL agents before synthesizing** — never post partial results
- **Respect `--no-post`** — if specified, only output to terminal

### Conciseness
- **Max ~50 lines** — the entire posted comment must fit in ~50 lines
- **Max 10 items** — combine 🔴 + 🟡; if more than 10, keep only highest-impact
- **No 🟢 Nice to Have** — never include green/nice-to-have items. Drop them entirely.
- **Pre-existing issues go in 📋 section** — they are flagged for awareness/backlog but do NOT block the PR or affect the overall verdict
- **One line per item** — `file:line` + ≤15 word fix description. No multi-line explanations.
- **Single comment always** — never split into multiple comments

### Idempotency
- **Check for previous reviews** — before posting, check if a previous `/review` comment exists on the PR
- **No new findings on unchanged code** — if the diff hasn't changed since the last review, do NOT post
- **Delta-only on re-review** — if code changed, post only NEW findings from the changed code
- **Self-review before posting** — re-read the draft; drop any item that is subjective, borderline, or wouldn't survive a re-run

### Evidence
- **No hallucinated findings** — agents must reference specific files and lines from the actual diff
- **Evidence over theory** — live environment findings with log evidence take priority over theoretical findings
- **DB queries are read-only** — only use production read replicas
- **Live validation is best-effort** — if Datadog/DB unavailable, proceed with agent results only
