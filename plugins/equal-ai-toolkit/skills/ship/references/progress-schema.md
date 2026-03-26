# Progress File Schema

Progress files live at `~/.claude/ship-state/<ticket-id>/progress.md` and track the full pipeline state across sessions.

## Format

```markdown
---
ticket: EQ-1234
feature: <feature name>
started: 2026-03-12
current_loop: design | implement | consolidate | phased_prs
status: in_progress | blocked | completed
---

## Loop 1: Design
- status: completed | in_progress | not_started
- iteration: 3/5
- confluence_url: <url>
- confluence_page_id: <id>
- affected_repos: [user-services, lambdas, cdk]
- design_verdict: APPROVED | NEEDS_REVISION | NOT_STARTED
- blockers: []

## Loop 2: Implementation
- status: in_progress | completed | not_started
- repos:
  - user-services: { branch: feature/EQ-1234-..., pr: #783, review: APPROVED, test: PASS }
  - lambdas: { branch: feature/EQ-1234-..., pr: #14, review: iteration_2, test: not_started }
  - cdk: { branch: feature/EQ-1234-..., pr: #55, review: APPROVED, test: PASS }
- subtasks:
  - EAI-1390: { component: "DB Schema + Models", status: Done }
  - EAI-1391: { component: "Service Logic", status: Done }
- blockers: ["lambdas review comment: batch_write unmarshalled items"]

## Loop 3: Consolidation
- status: not_started | in_progress | completed
- cross_repo_check: pending | pass | fail
- integration_test: pending | pass | fail
- requirements_coverage: pending | full | gaps_found
- deployment_order: []
- merge_order: []
- final_verdict: SHIP | BLOCKED | NOT_STARTED

## Loop 4: Phased PRs
- status: not_started | in_progress | completed | skipped
- orchestrator_pr: <url>
- phase_prs:
  - user-services: [phase-1-schema: #801, phase-2-data-layer: #802, ...]
  - cdk: [] (skipped — under 500 lines)
```

## State Transitions

The orchestrator writes to the progress file at every state transition:

| Event | Fields Updated |
|-------|---------------|
| Pipeline starts | `ticket`, `feature`, `started`, `current_loop: design`, `status: in_progress` |
| Design iteration completes | `Loop 1 > iteration` |
| Design published | `Loop 1 > confluence_url`, `confluence_page_id` |
| Design approved (Gate 1) | `Loop 1 > status: completed`, `design_verdict: APPROVED`, `current_loop: implement` |
| PR created | `Loop 2 > repos > <repo> > branch, pr` |
| Review verdict | `Loop 2 > repos > <repo> > review` |
| Test result | `Loop 2 > repos > <repo> > test` |
| Implementation complete (Gate 2) | `Loop 2 > status: completed`, `current_loop: consolidate` |
| Cross-repo check | `Loop 3 > cross_repo_check` |
| Integration test | `Loop 3 > integration_test` |
| Requirements coverage | `Loop 3 > requirements_coverage` |
| Subtask created | `Loop 2 > subtasks > <key> > component, status: To Do` |
| Subtask in progress | `Loop 2 > subtasks > <key> > status: In Progress` |
| Subtask done | `Loop 2 > subtasks > <key> > status: Done` |
| Consolidation complete (Gate 3) | `Loop 3 > status: completed`, `final_verdict: SHIP`, `current_loop: phased_prs` |
| Phased PRs started | `Loop 4 > status: in_progress` |
| Phase PR created | `Loop 4 > phase_prs > <repo> > append phase PR` |
| Phased PRs complete | `Loop 4 > status: completed`, `orchestrator_pr: <url>` |
| Phased PRs skipped | `Loop 4 > status: skipped` (all PRs under 500 lines) |
| Pipeline complete | `status: completed`, `final_verdict: SHIP` |
| Blocker encountered | `status: blocked`, relevant `blockers` array |

## Resume Protocol

When `/ship` is invoked with a ticket that has an existing progress file:

1. Read `~/.claude/ship-state/<ticket-id>/progress.md`
2. Parse the frontmatter for `current_loop` and `status`
3. Present current status to user in a summary table
4. Ask: "Resume from where we left off, or start fresh?"
5. If resume: jump to the `current_loop` and continue from last known state
6. If start fresh: archive the old progress file (rename with timestamp suffix) and create new one

## Reading Progress

To parse the progress file:
- Frontmatter (between `---` markers) contains the top-level state
- Each `## Loop N` section contains that loop's detailed state
- Bullet points use `key: value` format for easy parsing
- Arrays use `[item1, item2]` format
- Nested objects (repos) use `{ key: value, key: value }` format

## Writing Progress

When updating progress, read the full file, update the relevant fields, and write the entire file back. Don't append — always overwrite with the complete updated state.

## Session Locking

The progress file supports concurrent session safety via a lock file.

### Lock File: `~/.claude/ship-state/<ticket-id>/session.lock`

```markdown
session_id: <unique-id>
started: 2026-03-12T14:30:00
pid: <process-id>
loop: design
```

### Acquire Lock Protocol

Before starting or resuming work on a ticket:

1. Check if `session.lock` exists
2. If it exists:
   - Read the lock file
   - Check if the locking process is still alive: `kill -0 <pid> 2>/dev/null`
   - If process is **dead**: stale lock — delete it and proceed
   - If process is **alive**: **another session is actively working on this ticket**
     - Present to user: "Session `<session_id>` (PID <pid>) is currently working on this ticket since <started>. It's in the `<loop>` loop."
     - Ask: "Force take over (kills the other session's lock), or abort?"
     - If force: delete lock, create new one
     - If abort: exit without changes
3. If no lock exists: create the lock file

### Create Lock

```bash
echo "session_id: $(uuidgen)\nstarted: $(date -u +%Y-%m-%dT%H:%M:%S)\npid: $$\nloop: <current_loop>" > ~/.claude/ship-state/<ticket-id>/session.lock
```

### Update Lock

Update the `loop` field whenever `current_loop` changes in the progress file:
```bash
sed -i '' "s/^loop: .*/loop: <new_loop>/" ~/.claude/ship-state/<ticket-id>/session.lock
```

### Release Lock

Delete the lock file when:
- Pipeline completes (Step 6)
- Pipeline is blocked and waiting for user (gates)
- Session exits cleanly

```bash
rm -f ~/.claude/ship-state/<ticket-id>/session.lock
```

### Important

- The lock is **advisory** — it prevents accidental conflicts, not malicious ones
- PID check handles the common case of a session crashing without cleanup
- The lock is released at gates (user approval points) so another session could pick up from there
- Sub-skills (`/ship-design`, `/ship-implement`, `/ship-consolidate`) inherit the parent's lock — they don't create their own
