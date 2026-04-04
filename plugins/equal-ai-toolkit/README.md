# Equal AI Toolkit

Complete development toolkit for the Equal AI platform — from incident investigation to end-to-end feature delivery.

## Prerequisites

Configure these MCP servers in Claude Code:

1. **Datadog MCP** (required for investigation, monitoring, standups)
2. **Slack MCP** (required for standups, alert investigation)
3. **Atlassian MCP** (required for ship pipeline, standups)

## Installation

```bash
/plugin marketplace add inifity-tech/equal-ai-claude-skills
/plugin install equal-ai-toolkit@equal-ai-claude-skills
```

## Skills

### SRE & Operations

| Skill | Trigger | Description |
|-------|---------|-------------|
| `/investigate-alert` | Alert fires, "check prod", "something is broken" | Expert SRE incident investigation with parallel subagents, evidence-backed RCA reports |
| `/deploy-monitor` | After any production deployment | Post-deployment monitoring — ALB, ECS, logs, RDS, Redis, SQS/SNS, Datadog monitors, Slack alerts |
| `/create-monitor` | "create a monitor", "alert me when", "set up monitoring" | Create, list, update, delete Datadog monitors — researches code flow and production data to build context-rich alerts with deep links, sample logs, and investigation steps |
| `/cdk-deploy` | Infrastructure changes needed | CDK synth, diff, deploy with auto-recovery for SSM mismatches, missing ECR images |
| `/deploy-branch` | "deploy to maxtest", "push to staging", "update env var on preprod", "deploy and enable the feature gate" | Deploy feature branch to test/maxtest/preprod/typists via CodePipeline, or update env vars without code deploy — handles staging merge, task def updates, Statsig feature flag management, parallel multi-service deploys, Datadog validation |

### Code Review

| Skill | Trigger | Description |
|-------|---------|-------------|
| `/review` | PR review, "review this", branch review | Architecture review — spawns 4 parallel agents (HLD, LLD, NFR, LLM), runs LSP static analysis, validates against live environments (Datadog logs, DB state), posts synthesized report to GitHub |

### Feature Delivery (Ship Pipeline)

| Skill | Trigger | Description |
|-------|---------|-------------|
| `/ship` | End-to-end feature delivery | Orchestrates design → implement → consolidate → phased PRs from a Jira ticket |
| `/ship-design` | Design phase | Production-grounded design loop — Datadog/DB/code discovery, 4 parallel design agents, review iterations |
| `/ship-implement` | Implementation phase | Multi-repo parallel implementation with agent teams, review cycles, E2E tests |
| `/ship-consolidate` | Consolidation phase | Cross-repo validation — resource names, event schemas, API contracts, PII handling |
| `/ship-test` | E2E testing phase | Design-driven multi-service E2E testing against staging infrastructure |
| `/raise-phased-prs` | Large PRs need splitting | Break large feature branches into phased, reviewable PRs with orchestrator PR |

### Technical Exploration

| Skill | Trigger | Description |
|-------|---------|-------------|
| `/tech-advisor` | "deep dive", "let's explore", "help me think through", "should we use X or Y" | Staff-engineer-level technical discussion partner — combines codebase analysis, Datadog production data, DB validation, and web research for informed architectural conversations |

### Testing

| Skill | Trigger | Description |
|-------|---------|-------------|
| Integration Test Expert | "integration test", "e2e test", "test my API" | Production-informed integration test generation using Datadog traffic analysis |

### Team

| Skill | Trigger | Description |
|-------|---------|-------------|
| `/standup` | "standup", "daily digest", "team update" | Daily standup digest from Jira, GitHub PRs, Slack, AWS CloudTrail, Statsig |

## First-Run Setup

```bash
/investigate-alert
```

You'll be asked for AWS profile, DB credentials. Infrastructure values are pre-configured.

> **Note**: Config at `.claude/config/toolkit-config.yaml` contains credentials — add to `.gitignore`.

## Configuration

After setup, config is at `.claude/config/toolkit-config.yaml`:

```yaml
aws:
  profile: your-profile
  region: ap-south-1

database_credentials:
  username: your-db-username
  password: your-db-password

infrastructure:
  ecs_cluster: EAI00EqualAiServicesCluster00prod
  alb_name: EAI00equalai-shared-alb00prod

services:
  - name: ai-backend
    # ...
```
