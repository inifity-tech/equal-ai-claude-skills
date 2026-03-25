# Equal AI Claude Skills

Production-grade Claude Code skills for SRE investigation and integration testing.

## Available Tools

| Tool | Type | Description |
|------|------|-------------|
| `/investigate-alert` | Command | SRE alert investigation with Datadog, CloudWatch, code analysis. Produces RCA reports. |
| `integration-test-expert` | Skill | Auto-triggers when discussing integration tests. Uses production data to inform coverage. |

## Prerequisites

Before installing, configure these MCP servers in Claude Code:

1. **Datadog MCP** (required)
   - Run `/mcp` → Add Datadog server with API credentials

2. **Slack MCP** (optional, for auto-fetching alerts)
   - Run `/mcp` → Add Slack server

## Installation

```bash
/plugin marketplace add inifity-tech/equal-ai-claude-skills
/plugin install equal-ai-toolkit@equal-ai-claude-skills
```

## First-Run Setup

After installation, run:

```bash
/investigate-alert
```

You'll be asked three questions:
- **AWS CLI profile name** (e.g., `ai-prod-read`)
- **Database username** (e.g., `equalreadonly`)
- **Database password**

All Equal AI infrastructure values (ECS cluster, services, database hosts) are pre-configured.

> **Note**: The config file contains credentials. Add `.claude/config/toolkit-config.yaml` to your `.gitignore`.

Configuration is saved to `.claude/config/toolkit-config.yaml` in your project.

## Usage

### SRE Investigation

```bash
# Investigate latest alert from Slack
/investigate-alert

# Investigate specific issue
/investigate-alert "5xx errors on api-gateway since 14:00"

# Investigate by Datadog monitor ID
/investigate-alert 12345678
```

### Integration Test Expert

The skill triggers automatically when you mention:
- "create integration tests"
- "integration test suite"
- "test my API"
- "e2e test"

## What's Included

### SRE Investigation (`/investigate-alert`)

Structured 4-phase alert investigation:
1. **Gather**: Parse alert, build context
2. **Investigate**: Parallel subagents for infra + application
3. **Validate**: Build evidence chain, test alternatives
4. **Report**: Structured RCA with deep links

Uses:
- **Datadog**: Logs, metrics, APM traces, monitors
- **AWS CloudWatch**: ALB, ECS, RDS, Redis metrics
- **Code Analysis**: Trace through source code
- **Slack**: Fetch alerts from channels

### Integration Test Expert

Builds comprehensive test suites by:
- Analyzing production traffic via Datadog
- Mapping API flows and dependencies
- Generating pytest tests with real DB/Redis fixtures

## Configuration

After setup, config is at `.claude/config/toolkit-config.yaml`:

```yaml
aws:
  profile: your-prod-profile
  region: ap-south-1

services:
  - name: user-service
    datadog_tag: user-service
    ecs_name: user-service-prod
    code_path: services/user-service/app/
    test_path: services/user-service/tests/integration/
    database: user_service_db

# ... more settings
```

See [plugins/equal-ai-toolkit/config/example-config.yaml](plugins/equal-ai-toolkit/config/example-config.yaml) for full example.

## Requirements

- **Datadog MCP Server**: For logs, metrics, traces
- **AWS CLI**: With read-only production profile
- **Slack MCP Server**: Optional, for fetching alerts

## Updating

To get the latest version:

```bash
/plugin marketplace update equal-ai-claude-skills
```

## License

MIT
