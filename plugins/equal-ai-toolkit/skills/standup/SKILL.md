---
name: standup
description: Generate a daily standup digest by aggregating activity from Jira, GitHub PRs, Slack channels, AWS CloudTrail/CodePipeline, and Statsig feature flag changes, attributing work to specific Jira EPICs. Use when the user asks for a standup, daily digest, team update, or sprint status.
disable-model-invocation: false
---

# Daily Standup Digest Skill — EPIC-Centric

Generates **concise** standup updates per EPIC. One line per person per ticket. No fluff.

## Parameters

`$ARGUMENTS` can include:
- Team name or Jira project key (default: `EAI`)
- `--lookback N` days (default: 1, or 3 on Mondays)
- `--post-to-slack` to also post to Slack
- `--dry-run` to skip posting anywhere
- `--adhoc-epic EAI-XXXX` for unattributed work (default: `EAI-1` Production Bugs)

Example: `/standup`, `/standup --dry-run`, `/standup --lookback 2`

---

## How It Works Day-Over-Day (Continuity Model)

Each standup run posts a **new comment** on each EPIC. It does NOT edit previous comments.

### Daily flow:

```
Day 1 (first run):
  → No previous standup comment exists
  → Report current state only (what happened today)
  → Post comment on each EPIC with "📊 UPDATE — [Date]"

Day 2:
  → Find previous standup comment on each EPIC (posted Day 1)
  → Gather new activity since Day 1 (lookback window)
  → Compare: what was reported yesterday vs what's new today
  → ONLY report NEW activity — skip anything already covered in Day 1's comment
  → Post a NEW comment (not edit the old one)

Day N:
  → Same as Day 2 — always read the most recent standup comment, diff against it
```

### Deduplication rules:

1. **Read the previous standup comment** on each EPIC before generating the new one
2. **Extract items already reported** — PR numbers, ticket status changes, Statsig changes, deploy events
3. **Only include NEW items** — if PR #609 was reported yesterday, don't mention it again today
4. **Status continuity** — if a ticket was "In Progress" yesterday and is still "In Progress" today with no new comments/PRs, skip it entirely
5. **Carry forward blockers** — if a blocker was reported yesterday and is STILL active today, mention it again with duration: "🚨 BLOCKED — [reason] (day 2)"

### How to detect the previous standup comment:

On each EPIC, scan comments (newest first) for the pattern:
- Starts with `📊 UPDATE —` or `🔗 UNATTRIBUTED —`
- Contains structured lines like `@Person: [EAI-XXX]`
- Posted by the standup bot/user account

Extract from previous comment:
- **Date** — to confirm it's from the previous run
- **PR numbers mentioned** — to avoid re-reporting
- **Ticket statuses mentioned** — to detect changes
- **Statsig items mentioned** — to avoid re-reporting
- **Blockers** — to track duration

---

## Overview

```
1. Determine lookback window + compute epoch timestamps
2. Read previous standup comments from each EPIC
3. Gather all activity in parallel (Jira, GitHub, Slack, AWS, Statsig)
4. For each EPIC: diff new vs already-reported, generate concise update
5. Unattributed work → EAI-1 (Production Bugs)
6. Post or output
```

---

## Step 1: Determine Lookback Window

- **Tue–Fri:** 1 day | **Monday:** 3 days | Override with `--lookback N`
- Timezone: `Asia/Kolkata`
- Compute `since_date` as ISO 8601 string

**CRITICAL — Compute epoch timestamps dynamically:**

```bash
# Compute since_date and Unix epoch in milliseconds (for Statsig filtering)
python3 -c "
from datetime import datetime, timezone, timedelta
ist = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(ist)
# Monday = 0, so check weekday
lookback = 3 if now.weekday() == 0 else 1
since = (now - timedelta(days=lookback)).replace(hour=0, minute=0, second=0, microsecond=0)
print(f'SINCE_DATE={since.isoformat()}')
print(f'SINCE_EPOCH_MS={int(since.timestamp() * 1000)}')
print(f'SINCE_EPOCH_S={int(since.timestamp())}')
"
```

Use `SINCE_EPOCH_S` for Slack `oldest` parameter and `SINCE_EPOCH_MS` for Statsig timestamp comparisons. **NEVER hardcode epoch values.**

## Step 2: Read Previous Standup Comments

For each EPIC found in Step 3a, check its comments (newest first) for the most recent standup comment. This is used for deduplication in Step 4.

## Step 3: Gather All Raw Activity (in parallel)

### 3a. Jira — Tickets and comments

```
Tool: mcp__claude_ai_Atlassian__searchJiraIssuesUsingJql
JQL: project = EAI AND sprint in openSprints() AND status NOT IN ("Done") ORDER BY assignee, priority DESC
Fields: summary, status, issuetype, assignee, parent, duedate, updated, comment
maxResults: 100
```

**CRITICAL — Handle pagination:**
- The JQL may return more than 100 tickets. Check `totalCount` vs number of nodes returned.
- If `totalCount > len(nodes)`, use the `nextPageToken` from the response to fetch subsequent pages.
- Keep fetching until ALL tickets are collected. Missing pages = missing EPICs.

Extract per ticket: key, summary, status, assignee, parent EPIC, recent comments (since lookback), blocker keywords.

**CRITICAL — Also fetch comments on parent EPICs themselves:**
- After identifying all parent EPICs from child tickets, fetch each EPIC's own comments using `mcp__claude_ai_Atlassian__getJiraIssue` with `fields: ["comment"]`
- Filter EPIC-level comments by the lookback window (same as child ticket comments)
- These comments often contain standup-style updates, action items, and status reports posted directly on the EPIC
- Include EPIC-level comments in the activity analysis for that EPIC — they count as activity for Sprint Pulse and should be reported in the standup comment
- Do NOT confuse EPIC-level comments with the standup bot's own `📊 UPDATE` comments — skip those during activity analysis

**Orphan tickets (no parent EPIC):**
- Some tickets in the sprint may have no parent EPIC assigned.
- Collect these separately and report them under **EAI-1 (Production Bugs)** alongside unattributed work.
- Format: `@Person: [EAI-XXX] "summary" — no parent EPIC assigned, status: [status]`

### 3b. GitHub — PRs across all repos

```bash
# Merged PRs since lookback
for repo in myequal-ai-backend myequal-ai-user-services memory-service myequal-ai-cdk myequal-ai-lambdas myequal-ai-app myequal-ai-ios-app Internal-Dashboard myequal-post-processing-service myequal-evaluations myequal-ai-common myequal-api-gateway common-lambdas prompts-registry internal-tools internal-scripts myequal-ai-deployments; do
  gh pr list --repo inifity-tech/$repo --state merged --search "merged:>=${since_date}" \
    --json number,title,author,mergedAt,url,headRefName --limit 50
done

# Open PRs (in review)
for repo in myequal-ai-backend myequal-ai-user-services memory-service myequal-ai-cdk myequal-ai-lambdas myequal-post-processing-service myequal-ai-app myequal-ai-ios-app; do
  gh pr list --repo inifity-tech/$repo --state open \
    --json number,title,author,url,headRefName,createdAt --limit 30
done
```

**Attribute PRs to tickets** using the Deterministic Attribution Algorithm (see below).

### 3c. Slack — Channels only (NEVER DMs)

**Search 1 — General activity keywords:**
```
Tool: mcp__claude_ai_Slack__slack_search_public_and_private
Query: "update OR blocker OR blocked OR deployed OR incident" after:${since_date}
channel_types: "public_channel,private_channel"
```

**Search 2 — Per-person work messages in team channels:**
For each known team member, search for their messages in team channels since the lookback window:
```
Tool: mcp__claude_ai_Slack__slack_search_public_and_private
Query: "from:@{display_name} after:${since_date}"
channel_types: "public_channel,private_channel"
include_context: false
limit: 10
```
This catches standup-style updates, PR review requests, technical discussions, and status messages that don't use generic keywords. Attribute these to EPICs using the same attribution algorithm (ticket keys in message text → assignee's in-progress tickets → keyword matching against EPIC/ticket summaries).

**Search 3 — Ticket key mentions:**
If specific ticket keys or EPIC names are found in Jira activity, also search Slack for those keys:
```
Tool: mcp__claude_ai_Slack__slack_search_public_and_private
Query: "EAI-XXXX after:${since_date}"
channel_types: "public_channel,private_channel"
```

**Read prod changelog explicitly** using **dynamically computed `SINCE_EPOCH_S`** as the `oldest` parameter:

```
Tool: mcp__claude_ai_Slack__slack_read_channel
channel_id: C0944J9J2MT  (#equalai-prod-change-log)
oldest: ${SINCE_EPOCH_S}
limit: 30
```

**Known team channels** (read if search results are sparse):
- `C08DLS10H2N` — #equalai-assistant (daily standups, status updates)
- `C095UJYJVK2` — #equal-ai-tech (technical discussions, PR reviews)

**Privacy rule:** NEVER search or include DMs (im) or group DMs (mpim).

### 3d. AWS — CloudTrail + CodePipeline

```bash
# CloudTrail write events per user
aws cloudtrail lookup-events --profile ai-dev --region ap-south-1 \
  --lookup-attributes AttributeKey=Username,AttributeValue=<user>@equal.in \
  --start-time "${since_date}" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 50 --output json
```

Filter: keep `Create*`, `Update*`, `Put*`, `Delete*`, `Start*`, `Stop*`, `Modify*`, `Run*`. Skip read-only.

```bash
# CodePipeline executions
aws codepipeline list-pipeline-executions --pipeline-name "<pipeline>" \
  --profile ai-dev --region ap-south-1 \
  --query "pipelineExecutionSummaries[?lastUpdateTime>='${since_date}'].[status,lastUpdateTime,trigger.triggerType,trigger.triggerDetail]" \
  --output text
```

Pipelines: `EAI00<service>00<env>` for all services/envs.

### 3e. Statsig — Feature flag and config changes

Fetch and filter using **dynamically computed `SINCE_EPOCH_MS`**:

```bash
# Save responses to temp files, then filter by timestamp
for type in gates dynamic_configs experiments; do
  curl -s -H "statsig-api-key: console-lLy4xFhll4sjbjeRcraw0qQHqm10BkZij5JMDFUdOUe" \
    "https://statsigapi.net/console/v1/${type}" -o "/tmp/statsig_${type}.json"
done

# Filter all three by SINCE_EPOCH_MS
python3 -c "
import json
since_ts = ${SINCE_EPOCH_MS}
for ftype in ['gates', 'dynamic_configs', 'experiments']:
    with open(f'/tmp/statsig_{ftype}.json') as f:
        data = json.load(f)
    for item in data.get('data', []):
        mt = item.get('lastModifiedTime', 0)
        if mt >= since_ts:
            print(f'{ftype}: {item[\"name\"]} | {item.get(\"lastModifierName\",\"?\")} | {mt}')
"
```

**Attribute Statsig changes to EPICs** by matching:
1. Gate/config name contains ticket key (e.g., `eai_1289_gemini_vad`)
2. Gate/config description references ticket
3. If no match → attribute by modifier name → person → their tickets

## Step 4: Per-EPIC Analysis (with Deduplication)

For each EPIC with activity:
1. Collect: **EPIC-level comments** (from Step 3a), child ticket comments, attributed PRs, deploys, Statsig changes, blockers
2. **Read previous standup comment** (from Step 2)
3. **Remove already-reported items**: if PR #609 was in yesterday's comment, skip it
4. **Only output genuinely new activity since last standup**
5. If NO new activity after deduplication → skip this EPIC entirely (don't post)

### 4a. Compute Sprint Pulse per EPIC

For each EPIC, compute:
- **in_progress_count**: number of child tickets with status = "In Progress"
- **active_count**: of those, how many have at least one comment, status transition, or attributed PR/deploy in the **last 3 calendar days** (this is a fixed 3-day window, NOT the lookback window)
- **stale_in_progress**: list of tickets that are "In Progress" but have NO activity in the last 3 days — capture: assignee name, ticket key, ticket summary, days since last activity

This data is used in Step 5 to render the Sprint Pulse header and Stale In-Progress callout.

## Step 5: Generate Per-EPIC Comment

**CRITICAL: Keep it extremely concise. One line per person. Max 1-2 sentences per line item. The whole comment should be scannable in 10 seconds.**

Format (EPIC with activity):

```
📊 UPDATE — [Date]

📈 SPRINT PULSE: [active_count]/[in_progress_count] In-Progress tickets updated in last 3 days ([percentage]%)

⚠️ STALE IN-PROGRESS — These tickets are marked In Progress but have had no activity for 3+ days:
  • @Person — [EAI-XXX]: ticket summary (N days silent)
  • @Person — [EAI-YYY]: ticket summary (N days silent)

━━━

@Person1: [EAI-XXX] summary of what changed — PR #N merged / deployed to env / status moved to X
@Person1: [EAI-YYY] one-line status
@Person2: [EAI-ZZZ] one-line status
🚨 @Person3: [EAI-AAA] BLOCKED — reason (day N)

Statsig: gate_name toggled ON for 10% by @Person
Deploys: service→env (status) by @Person

[N] tickets progressed | [N] stale | [N] blocked
```

**Sprint Pulse rules:**
- Always render `📈 SPRINT PULSE` as the first line after the date header — even if 0/0 (show "0/0 (0%)")
- The 3-day activity window is fixed and independent of the `--lookback` parameter
- Activity = any of: Jira comment, status transition, attributed PR (merged or opened), deploy, Statsig change
- Only count tickets with status exactly "In Progress" (not "To Do", "In Review", "Blocked", "Done")

**Stale In-Progress rules:**
- Only render the `⚠️ STALE IN-PROGRESS` section if there is at least one stale ticket
- List each stale ticket on its own line with `@AssigneeName`, ticket key, summary, and days since last activity
- This is a **call-out** — the purpose is to name people so the standup discussion can address them
- If all In-Progress tickets have recent activity, omit this section entirely
- The `━━━` separator always appears between the pulse header and the activity lines

Format (EPIC with NO activity):

```
📊 UPDATE — [Date]

📈 SPRINT PULSE: 0/[in_progress_count] In-Progress tickets updated in last 3 days (0%)

⚠️ STALE IN-PROGRESS — These tickets are marked In Progress but have had no activity for 3+ days:
  • @Person — [EAI-XXX]: ticket summary (N days silent)
  • @Person — [EAI-YYY]: ticket summary (N days silent)

━━━

No new updates detected.
Sources checked: Jira comments, GitHub PRs (17 repos), #equalai-prod-change-log, Statsig, AWS CloudTrail
Active tickets: [list ticket keys and assignees still in progress]

This could be an attribution gap — please let me know if this work has been covered as part of another EPIC.
```

**Tagging format:** Use `@DisplayName` (plain @ + Jira display name) for person names. The MCP tool accepts Markdown — Jira wiki markup `[~accountId:...]` does NOT work (brackets get escaped). Do NOT use `[~accountId:...]` or `**@Name**` syntax — just plain `@name`.

**Rules for conciseness:**
- NO section headers like "Completed", "In Progress", "Blockers" — just prefix blockers with 🚨
- NO elaboration — if it takes more than one line, it's too long
- NO next actions or suggestions — only report what happened
- NO health metrics paragraphs — just the one-line summary at the bottom
- One `@Person: [TICKET] what changed` per line, that's it
- If a person had no NEW activity on their assigned tickets, don't mention them
- If a ticket had no NEW updates since last standup, don't mention it
- Only mention Statsig changes if they actually happened AND weren't reported yesterday
- Blockers that persist from yesterday get a duration tag: "(day N)"
- EPICs with NO activity still get a comment — list active tickets so nothing is silently ignored

## Step 6: Unattributed Work

Collect work not matched to any EPIC. Post on **EAI-1 (Production Bugs)** as the default ad-hoc EPIC.

Format (same concise style):
```
🔗 UNATTRIBUTED — [Date]
@Person: PR #N "[title]" in repo — merged/open
@Person: Statsig gate_name changed
@Person: deployed service→env
```

## Step 7: Post or Output

**CRITICAL — Post to EVERY EPIC, not just active ones:**
You MUST post a comment to **every EPIC discovered in Step 3a**, regardless of whether new activity was found. EPICs with no new activity get the "No updates detected" format from Step 5. Track two lists explicitly:
1. `epics_with_activity` — post the activity comment
2. `epics_without_activity` — post the "No updates detected" comment with Sprint Pulse and active ticket list
Before finishing, verify: `len(epics_with_activity) + len(epics_without_activity) == total EPICs discovered`. If not, you have missed EPICs — go back and post to the missing ones.

- **Default:** Post per-EPIC comments via `mcp__claude_ai_Atlassian__addCommentToJiraIssue`
- **`--post-to-slack`:** Also post consolidated digest to Slack channel
- **`--dry-run`:** Output to console only, post nothing

## Step 8: Output Summary

```
DONE — [Date] | [N] EPICs updated ([N] with activity + [N] no activity) | [N] PRs | [N] deploys | [N] blocked | [N] unattributed
```

**Verification:** The total EPICs updated MUST equal the total EPICs discovered in Step 3a. If it doesn't, list the missing EPICs and post to them before reporting DONE.

---

## Guard Rails

- **Never fabricate** — only report what is found in data sources
- **Attribute conservatively** — no ticket match = unattributed → EAI-1
- **Respect lookback** — no data outside the window
- **Graceful degradation** — if a source fails, continue with others
- **Privacy** — NEVER DMs. Only `channel_types: "public_channel,private_channel"`
- **Monday logic** — expand to 3 days
- **First run** — if no previous standup comment, just report current state
- **Brevity is mandatory** — one line per person per ticket. No exceptions.
- **Only report NEW changes** — skip tickets/people with no activity since lookback
- **Never hardcode epochs** — always compute dynamically from current date
- **Deduplication is mandatory** — never re-report items from yesterday's standup
- **No silent EPICs** — every EPIC in the sprint gets a comment, even if "No updates detected"
- **No wiki markup** — use plain `@DisplayName` not `[~accountId:...]` for mentions
- **Paginate JQL results** — if totalCount > returned nodes, fetch ALL pages using nextPageToken. Never assume 100 tickets is the full set.
- **Handle orphan tickets** — tickets with no parent EPIC go under EAI-1 (Production Bugs)

## GitHub Repo List

Org: `inifity-tech`

**Core:** myequal-ai-backend, myequal-ai-user-services, memory-service, myequal-post-processing-service, myequal-ai-app, myequal-ai-ios-app, Internal-Dashboard
**Infra:** myequal-ai-cdk, myequal-ai-lambdas, myequal-ai-deployments, common-lambdas
**Supporting:** myequal-evaluations, myequal-ai-common, myequal-ai-support, myequal-api-gateway, prompts-registry, internal-tools, internal-scripts

## AWS

- **Profile:** `ai-dev` | **Region:** `ap-south-1`
- **IAM pattern:** `<name>@equal.in`

## Statsig

- **Console API key:** `console-lLy4xFhll4sjbjeRcraw0qQHqm10BkZij5JMDFUdOUe`
- Use this key for all Console API calls (`/console/v1/gates`, `/console/v1/dynamic_configs`, `/console/v1/experiments`)
- The `secret-*` server key does NOT work for Console API — it returns 403

## Name Mapping

Known GitHub → Jira mappings:

| GitHub login | Jira display name |
|---|---|
| `AkshayByroju-EQ` | akshay |
| `swap-inf` | swapnil |
| `krishnac-equal` | Krishna.C |
| `Nishika3009` | nishika |
| `Ayush-Priyam` | Ayush Priyam |
| `vinaysshenoy` | Vinay Shenoy |
| `aldefy` | Adit Lal |
| `pavan-mk1` | pavan.m |
| `sandeep-equal` | Sandeep Kumar Sahu |
| `yogesh-equal` | Yogesh Pareek |
| `razzinfinity` | Raja |

Expand as new mappings are discovered during runs. Also match by email/IAM username → display name similarity → Jira account ID lookup.

## Deterministic EPIC Attribution Algorithm

This is the core attribution logic. Apply these rules **in priority order** — stop at the first match.

### Step 0: Build lookup tables FIRST (before attributing anything)

Before processing any PRs/activity, build these maps from the Jira data fetched in Step 3a:

```
TICKET_MAP: {ticket_key → {epic_key, assignee, summary, status}}
  — All child tickets in the sprint, keyed by ticket key (e.g., EAI-1230)

PERSON_TICKETS: {github_username → [ticket_keys]}
  — For each person, their assigned in-progress tickets (status = "In Progress" or "In Review")

GITHUB_TO_JIRA: {github_login → jira_display_name}
  — Use the known mappings table above, expand as discovered
```

### Step 1: Explicit ticket key in PR metadata (highest confidence)

Search for ticket key patterns (e.g., `EAI-1234`, `B2C-567`) in:
1. **Branch name** — e.g., `feature/EAI-1230-voice-activity`
2. **PR title** — e.g., `EAI-1230: Add voice activity detection`
3. **PR body/description** — any mention of ticket key

Regex: `[A-Z]{2,5}-\d{2,5}`

If found → map ticket key to its parent EPIC via `TICKET_MAP`. **Done.**

### Step 2: Assignee + repo/service mapping (medium confidence)

If no ticket key found:
1. Resolve PR author's GitHub login → Jira display name via `GITHUB_TO_JIRA`
2. Look up that person's in-progress tickets from `PERSON_TICKETS`
3. Match the **repo name** to the ticket's likely service area:

| Repo pattern | Service area |
|---|---|
| `myequal-ai-backend` | Backend / AI service |
| `myequal-ai-user-services` | User services |
| `memory-service` | Memory service |
| `myequal-ai-app` | Android app / frontend |
| `myequal-ai-ios-app` | iOS app / frontend |
| `Internal-Dashboard` | Dashboard / admin |
| `myequal-ai-cdk`, `myequal-ai-lambdas`, `common-lambdas` | Infrastructure |
| `myequal-post-processing-service` | Post-processing |
| `prompts-registry` | Prompts / LLM |
| `myequal-ai-deployments` | Deployments |

4. If the person has exactly **one** in-progress ticket related to that service area → attribute to that ticket's EPIC
5. If the person has **multiple** candidate tickets → check PR title/description for keywords matching any ticket summary. Pick the best match.
6. If still ambiguous → attribute to the EPIC with the **most recent activity** among candidates

### Step 3: PR title keyword matching against ticket summaries (low confidence)

If Steps 1-2 fail:
1. Tokenize the PR title (remove common words: "fix", "add", "update", "refactor", "chore", "feat", "wip")
2. Compare remaining tokens against all ticket summaries in the sprint
3. If a ticket summary shares 2+ significant keywords with the PR title → attribute to that ticket's EPIC
4. Mark this attribution with `(inferred)` in the output

### Step 4: Unattributed (no match)

If all steps fail → mark as unattributed. Post on EAI-1 (Production Bugs).

### Attribution for non-PR activity

- **AWS CloudTrail events**: Attribute by IAM username → person → their in-progress tickets → EPIC. If the event is a CodePipeline execution, map pipeline name (`EAI00<service>00<env>`) to the service, then to tickets for that service.
- **Statsig changes**: Match gate/config/experiment name against ticket keys (e.g., `eai_1289_gemini_vad` → `EAI-1289`). If no ticket key in name, use `lastModifierName` → person → their tickets.
- **Slack prod changelog messages**: Match mentioned ticket keys. If none, attribute by message author → person → their tickets. Use message content keywords to match EPIC themes.

### Attribution confidence tags

When outputting attributed items, optionally tag confidence:
- No tag = explicit ticket key match (Step 1)
- `(by-assignee)` = matched via assignee + service area (Step 2)
- `(inferred)` = keyword matching (Step 3)
