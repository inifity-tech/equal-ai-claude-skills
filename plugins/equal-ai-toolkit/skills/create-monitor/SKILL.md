---
name: create-monitor
description: "Creates, lists, updates, and deletes Datadog monitors with rich Slack alerts for Equal AI services. Before creating a monitor, it deep-dives into the service's code flow and analyzes production logs/metrics to craft alert messages packed with context — supporting log lines, related queries, deep links, and investigation steps. Use this skill whenever someone wants to: set up a new monitor or alert, manage existing Datadog monitors, add alerting for a service, or says things like 'monitor this', 'alert me when', 'create a Datadog monitor', 'I need an alert for', 'set up monitoring'. Also use when reviewing or cleaning up existing monitors."
version: 1.0.0
---

# Datadog Monitor Manager

Create high-signal Datadog monitors for Equal AI services. Research the service's code and production patterns first, then craft alerts that give on-call engineers everything they need to start investigating immediately.

## Output Style

Be concise throughout. Minimize cognitive overhead for the user and for whoever reads the alert.

- **Conversation**: Short status updates between phases. No narrating what you're about to do — just do it and report results. Skip filler like "Let me now..." or "Great, I'll proceed to...".
- **Alert messages**: Dense, scannable. Use tables and bullet points over paragraphs. Every line should carry information — no restating the monitor name/query in prose when it's already in the header. Sample log lines should be trimmed to the relevant parts.
- **Investigation steps**: 3-5 concrete steps max. Each step = one action + what to look for. No preamble, no "if you see X then Y" branching trees.
- **Confirm prompts**: Show the query, thresholds, and Slack target in a compact block. Don't repeat the full alert message — just note what it includes.

## Configuration

Read `.claude/config/toolkit-config.yaml` for service mappings, Slack channel (`slack.alerts_channel`), and AWS config. If missing, tell the user to run `/investigate-alert` first.

## Handling Input

| Input | Action |
|-------|--------|
| Service + description (e.g., "ai-backend high error rate") | **Create** — full research + create flow |
| `list` or `list <service>` | **List** monitors, optionally filtered |
| `update <monitor-id> <changes>` | **Update** existing monitor |
| `delete <monitor-id>` | **Delete** (with confirmation) |
| Empty | Ask what the user wants |

For **create**, extract: service (required), monitor name (required), threshold (required), monitor type (optional — infer from context), additional details (optional). Ask if anything critical is missing.

---

## Create Flow

### Phase 1: Resolve & Confirm

1. Resolve service from config — get `datadog_tag`, `ecs_name`, `code_path`, `database`
2. Confirm intent: "Monitoring {service} for {condition}, alerting at {threshold}. Sound right?"
3. Determine type: log patterns/errors → **Log monitor** | latency/CPU/memory → **Metric monitor** | slow endpoints/traces → **APM monitor** | combined conditions → **Composite**

### Phase 2: Research (Parallel Subagents)

Launch two `general-purpose` subagents in parallel:

**Subagent A — Code Flow:**
```
Research {service_name} code at {code_path} for: {what we're monitoring}.

1. Trace the relevant code path — find exact file:line where the condition originates
2. Map the call chain (entry point → intermediate → point of interest)
3. Check error handling, retries, fallbacks that might mask issues
4. Find related code paths that could produce similar errors from different causes
5. Check git log for last 2 weeks of changes to these files

Return: code flow summary, file:line refs, call chain, investigation steps grounded in the code.
```

**Subagent B — Production Data:**
```
Analyze production data for {service_name} ({datadog_tag}), monitoring: {description}, threshold: {threshold}.

Read .claude/config/toolkit-config.yaml for config.

1. Baseline the pattern over 24h — normal rate, variation, time-of-day patterns
2. Validate threshold — would it have fired? Too noisy? Too loose? Recommend adjustments
3. Gather 3-5 sample log lines matching the pattern from production
4. Find related log lines that accompany the condition (upstream requests, correlated errors)
5. Check existing monitors via mcp__datadog__get_monitors — duplicates? Related monitors to reference?

Return: baseline data, threshold validation, sample logs, related logs, existing monitors.
```

### Phase 3: Design the Monitor

Read `references/monitor-templates.md` for API schemas and query syntax.

**Query** — scope tightly by service, environment, and relevant tags. A noisy monitor is worse than no monitor.

**Alert message** — the investigation starting kit:

```
## {Monitor Name}
**Service:** {service_name} | **Env:** {{env.name}} | **Triggered:** {{last_triggered_at}}

### What's happening
{1-2 sentence plain-English description}

### What normal looks like
{Baseline from Subagent B}

### Current state
{{value}} (threshold: {threshold})

### Supporting evidence
**Log query:** [View in Datadog]({deep_link})

**Sample log lines from production:**
{3-5 real log lines from Subagent B}

**Related logs:**
- [View {pattern_1}]({deep_link_1})
- [View {pattern_2}]({deep_link_2})

### Code context
{Call chain from Subagent A with file:line refs}

### Investigation steps
1. {Code-grounded step from Subagent A}
2. {Step 2}
3. {Step 3}

### Related monitors
{Links from Subagent B}

@slack-{alerts_channel}
```

Use Datadog template variables (`{{value}}`, `{{threshold}}`, `{{last_triggered_at}}`) for dynamic values.

**Notification routing** — extract channel from `slack.alerts_channel`, format as `@slack-{channel_name_without_hash}`.

**Confirm before creating** — show the user the monitor name, query, thresholds, and full alert message.

### Phase 4: Create via API

```bash
source ~/.zshrc 2>/dev/null
curl -s -X POST "https://api.datadoghq.com/api/v1/monitor" \
  -H "Content-Type: application/json" \
  -H "DD-API-KEY: ${CLAUDE_DD_API_KEY}" \
  -H "DD-APPLICATION-KEY: ${CLAUDE_DD_APP_KEY}" \
  -d '{monitor_payload}'
```

**Always include in options:** `notify_no_data: true`, `no_data_timeframe: 10`, `renotify_interval: 60`, `include_tags: true`, warning threshold at ~70-80% of critical.

**Always tag:** `service:{name}`, `env:prod`, `team:equal-ai`, `created-by:claude-monitor-skill`

### Phase 5: Verify

Confirm via `mcp__datadog__get_monitors`, report the monitor URL (`https://app.datadoghq.com/monitors/{id}`), and summarize what the alert message includes.

---

## List Flow

1. Query `mcp__datadog__get_monitors` — try both name filter AND `tags: ["service:{name}"]` to catch all matches
2. Present as table: ID, Name, Type, Status, Query (truncated)
3. Highlight any tagged `created-by:claude-monitor-skill`

## Update Flow

1. Fetch current config, show it
2. Apply changes. If threshold changed, offer to re-run production analysis to refresh baseline context
3. `PUT` to `/api/v1/monitor/{id}`, confirm what changed

## Delete Flow

1. Show monitor details
2. **Confirm explicitly** — destructive, cannot be undone
3. `DELETE` `/api/v1/monitor/{id}` only after confirmation

---

## Guard Rails

- Confirm before any create/update/delete
- Check for duplicate monitors before creating
- Source `~/.zshrc` before API calls if env vars aren't set
- Tag everything: `service`, `env`, `team`, `created-by`
- Validate thresholds against production data — flag if it would fire constantly or never
- IST timestamps (UTC+5:30)
- Scope queries tightly — one service, not all

## Error Handling

| Error | Action |
|-------|--------|
| DD API 403/401 | Check `CLAUDE_DD_API_KEY` and `CLAUDE_DD_APP_KEY` env vars |
| Service not in config | List available services, ask user to pick |
| Bad threshold | Show baseline data, recommend adjustment |
| Duplicate detected | Show existing monitor, offer to update instead |
| Missing Slack channel | Ask user to add `slack.alerts_channel` to config |
