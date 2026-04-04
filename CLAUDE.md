# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

A Claude Code **plugin marketplace** (`equal-ai-claude-skills`) containing the `equal-ai-toolkit` plugin. The plugin provides Claude Code skills for the Equal AI platform — SRE investigation, feature delivery (ship pipeline), integration testing, deployment, and standups.

This is not application code. It's a collection of Markdown-based skill definitions (SKILL.md files) that Claude Code executes as structured prompts.

## Repository Structure

```
plugins/equal-ai-toolkit/
├── config/example-config.yaml   # Reference config for toolkit setup
├── skills/
│   ├── investigate-alert/       # SRE incident investigation + first-run setup
│   ├── deploy-monitor/          # Post-deployment monitoring
│   ├── cdk-deploy/              # CDK infrastructure deployment
│   ├── ship/                    # Meta-orchestrator: design → implement → consolidate → PRs
│   ├── ship-design/             # Production-grounded design loop
│   ├── ship-implement/          # Multi-repo parallel implementation
│   ├── ship-consolidate/        # Cross-repo validation
│   ├── ship-test/               # Multi-service E2E testing
│   ├── raise-phased-prs/        # Split large branches into reviewable PRs
│   ├── integration-test-expert/ # Test generation from Datadog traffic analysis
│   └── standup/                 # Daily standup digest
└── README.md
templates/CLAUDE.md.snippet      # Snippet for consumer projects to add to their CLAUDE.md
install.sh / uninstall.sh        # Legacy manual install scripts (prefer /plugin commands)
TESTING.md                       # Manual testing checklist for the plugin
```

## Skill Anatomy

Each skill lives in `plugins/equal-ai-toolkit/skills/<skill-name>/` and contains:
- **SKILL.md** — The skill definition with YAML frontmatter (`name`, `description`, `disable-model-invocation`) followed by the full prompt/instructions
- **references/** (optional) — Supporting Markdown files the skill reads at runtime (patterns, templates, query references)

Frontmatter fields:
- `name`: Skill identifier (used in `/skill-name` triggers)
- `description`: Natural language description (used by Claude Code for skill matching/triggering)
- `disable-model-invocation`: Whether the skill can be auto-triggered (`false` = yes it can be triggered)

## Key Patterns

- **Shared config**: Skills share a runtime config at `.claude/config/toolkit-config.yaml` in the consumer project. `/investigate-alert` owns the first-run setup flow; other skills redirect there if config is missing.
- **MCP dependencies**: Skills rely on external MCP servers (Datadog, Slack, Atlassian) — they call MCP tools directly in their instructions.
- **Ship pipeline**: `/ship` is a meta-orchestrator that calls the sub-skills (`ship-design`, `ship-implement`, `ship-consolidate`, `raise-phased-prs`) in sequence. Each sub-skill can also run standalone.
- **Subagent patterns**: Several skills (investigate-alert, ship-design, ship-implement) launch parallel subagents for concurrent work.
- **Reference files**: Skills read their `references/*.md` files at runtime for templates, query patterns, and domain knowledge.

## Installation & Testing

```bash
# Install via plugin marketplace (in Claude Code)
/plugin marketplace add inifity-tech/equal-ai-claude-skills
/plugin install equal-ai-toolkit@equal-ai-claude-skills

# Install from local path (for development)
/plugin install ~/equal-ai/claude-skills/plugins/equal-ai-toolkit

# Test a skill after changes
/investigate-alert
/ship EQ-1234
```

See `TESTING.md` for the full manual testing checklist covering setup flow, SRE investigation, and integration test expert.

## When Editing Skills

- Skill prompts are long structured Markdown — preserve section numbering and heading hierarchy
- Config keys referenced in skills must match `config/example-config.yaml` field names
- If adding a new skill, create `plugins/equal-ai-toolkit/skills/<name>/SKILL.md` with the standard frontmatter
- Update `plugins/equal-ai-toolkit/README.md` skill table when adding/removing skills
