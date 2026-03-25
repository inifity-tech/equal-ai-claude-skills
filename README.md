# Claude Skills

Production-grade Claude Code skills for SRE investigation and integration testing.

## Available Tools

| Tool | Type | Description |
|------|------|-------------|
| `/investigate-alert` | Command | SRE alert investigation with Datadog, CloudWatch, code analysis. Produces RCA reports. |
| `integration-test-expert` | Skill | Auto-triggers when discussing integration tests. Uses production data to inform coverage. |

## Installation

### For Your Team

```bash
# Clone the repo
git clone https://github.com/your-org/claude-skills.git ~/claude-skills

# Run the install script
~/claude-skills/install.sh
```

This copies the skills to `~/.claude/` where Claude Code can find them.

### Uninstall

```bash
~/claude-skills/uninstall.sh
```

## First-Run Setup

After installation, run:

```bash
/investigate-alert
```

On first run, you'll go through an interactive setup that configures:
- AWS profile and region
- ECS cluster and ALB names
- Service mappings (name, Datadog tag, code path, test path)
- Database connections
- Test environment settings

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
cd ~/claude-skills
git pull
./install.sh
```

## License

MIT
