---
name: handoff
description: Write or update a handoff document so the next agent with fresh context can continue this work. Use this skill whenever the user says "handoff", "hand off", "checkpoint", "save progress", "session handoff", "pass the baton", "context transfer", "save session", "wrap up session", or wants to preserve current work context for a fresh conversation.
---

# Session Handoff

Write or update `HANDOFF.md` so a fresh Claude Code session can resume this work with zero context loss.

## Steps

### 1. Determine handoff location

Get the current branch name via `git branch --show-current`. The handoff document lives at `./tmp/{branch-name}/HANDOFF.md`.

- Check if `./tmp/{branch-name}/` already exists
- If it does, check for an existing `HANDOFF.md` inside it — the previous session's context is valuable and should be preserved in a "Prior Sessions" section at the bottom
- If the directory doesn't exist, create it (`mkdir -p ./tmp/{branch-name}/`)

### 2. Gather context

Collect everything a fresh session would need. Run these in parallel:

**Git state:**
- `git status` — working tree changes, staged files, current branch
- `git diff` and `git diff --staged` — actual code changes in flight
- `git log --oneline -20` — recent commit history
- `git branch` — which branches exist

**Conversation context** — review the current conversation for:
- The original task or goal the user described
- Decisions made and their rationale
- Approaches that worked
- Approaches that failed and why
- Constraints or requirements discovered along the way
- Open questions or unresolved issues
- Blockers encountered

**Files touched** — identify all files that were read, created, or modified during this session.

### 3. Write HANDOFF.md

Create or overwrite `HANDOFF.md` in `./tmp/{branch-name}/` using this structure:

```markdown
# Session Handoff

*Generated: {date and time}*
*Branch: {current branch}*
*Repository: {repo path}*

## Goal

{What we're trying to accomplish — the high-level objective in 2-3 sentences}

## Context

{Repository, branch, environment, key files involved, any relevant background}

## Current Progress

{What's been done so far — specific file paths, commit hashes, concrete accomplishments}

## What Worked

{Approaches that succeeded — so they can be built upon}

## What Didn't Work

{Approaches that failed and why — so they're not repeated}

## Key Decisions

{Important decisions made during this session and their rationale}

## Open Questions

{Unresolved questions or uncertainties}

## Next Steps

{Clear, ordered action items for continuing the work}

## Files Modified

{List of all files changed in this session with one-line descriptions}

## Dependencies & Blockers

{Any external dependencies, blocking issues, or things to watch out for}

## Uncommitted Changes

{Summary of git diff — what's staged, what's unstaged, what's untracked}
```

If a previous `HANDOFF.md` existed, append its content under:

```markdown
---

## Prior Sessions

{Previous handoff content, preserved for history}
```

Omit any section that has nothing meaningful to report — don't write empty sections.

### 4. Report to user

Tell the user:
- The absolute file path to `./tmp/{branch-name}/HANDOFF.md`
- A one-line summary of what was captured
- Remind them to start the next session with: "Read ./tmp/{branch-name}/HANDOFF.md and continue the work"
