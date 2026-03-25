# RCA Report Template

Use this exact structure for the final report. Every section should contain actual evidence — no placeholder text.

```markdown
## Issue
{1-line summary that a non-engineer could understand}
{e.g., "Users couldn't make calls for 12 minutes because the voice processing service ran out of database connections"}

## Alert Context
- **Monitor**: {name} (ID: {monitor_id})
- **Trigger condition**: {exact query and threshold}
- **Trigger time**: {HH:MM IST}
- **Duration**: {how long the issue lasted, or "ongoing"}

## Time Period
- **From**: {HH:MM IST, DD MMM YYYY}
- **To**: {HH:MM IST, DD MMM YYYY} or **ongoing**

## Impact
- **Services affected**: {list}
- **User impact**: {what users experienced — e.g., "calls dropped mid-conversation", "app showed loading spinner for 30+ seconds"}
- **Scope**: {percentage of users/requests affected, if known}

## Classification
{APPLICATION | INFRASTRUCTURE | BOTH} — {brief justification}

## Timeline
| Time (IST) | Event | Evidence |
|------------|-------|----------|
| {HH:MM} | {what happened} | {where you found this — log entry, metric, deployment record} |
| {HH:MM} | {next event} | {evidence} |

## Root Cause
{Specific statement of what caused the alert. Not symptoms — the actual cause.}
{e.g., "A missing connection.close() in the error handling path of the /api/v1/calls endpoint caused database connections to leak under error conditions. When the Ultravox API returned intermittent 503s starting at 14:30, each failed request leaked a connection. After ~200 failures, the pool was exhausted."}

### Failure Chain
{Show the causal chain: A → B → C → alert}
```
{trigger event}
  → {first consequence}
    → {second consequence}
      → {what the user experienced / what the alert detected}
```

### Failure Logs
```
{2-3 complete, representative log entries with timestamps and stack traces}
{These should be the most diagnostic logs — the ones that tell the story}
```

**View in Datadog**: [{query description}]({deep_link_url})

### Evidence Chain
{Number each piece of evidence. For each, state what it is, what it shows, and how it supports the root cause.}

1. **[Logs]** {excerpt or summary} — proves {what}
2. **[Metrics]** {metric}: {value at trigger} vs {baseline} — shows {what}
3. **[Traces]** {trace detail} — reveals {what}
4. **[Code]** {file:line} — confirms {what}
5. **[Infra]** {resource: metric} — demonstrates {what}

### Correlated Metrics
| Metric | Value at Trigger | Baseline (24h avg) | Link |
|--------|-----------------|-------------------|------|
| {metric name} | {value} | {baseline} | [{source}]({url}) |

### Alternative Explanations Ruled Out
{For each alternative, explain specifically why the evidence rules it out}

- **Deployment**: {ruled out because — e.g., "last deployment was 6 hours before the incident, and the error pattern doesn't match any changed code paths"}
- **Traffic spike**: {ruled out because — e.g., "RequestCount was within normal range (±10% of daily average)"}
- **Downstream dependency**: {ruled out/confirmed — e.g., "Ultravox API was returning 503s, confirmed as the trigger event"}
- **Known issue**: {matches/doesn't match — e.g., "similar to JIRA-1234 but different root cause"}
- **Data issue**: {ruled out because — e.g., "no unusual data patterns in the affected tables"}

## Confidence Level
**{HIGH | MEDIUM | LOW}** — {justification}
{e.g., "HIGH — the connection leak is visible in the code, the timeline matches exactly, and the fix has been tested locally"}

## Action Items
### Immediate (now)
- [ ] {action} — {who should do this}

### Short-term (this week)
- [ ] {action} — {who/what team}

### Long-term (systemic fix)
- [ ] {action} — {what it prevents}

### Monitoring Improvements
- [ ] {what new alert/dashboard would have caught this sooner}
```

## Writing Guidelines

- **Be specific**: "error rate increased" → "error rate increased from 0.2% to 14.7%"
- **Include timestamps**: every event in the timeline needs a time
- **Link everything**: every metric reference gets a deep link
- **Show your work**: if you ruled something out, say what evidence ruled it out
- **Honest confidence**: if you're not sure, say MEDIUM or LOW and explain what additional data would raise confidence
- **Actionable items**: each action item should be specific enough that someone could start working on it without asking questions
