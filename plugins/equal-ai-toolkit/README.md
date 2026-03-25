# Equal AI Toolkit

SRE investigation and integration testing tools for the Equal AI platform.

## Prerequisites

Before using this plugin, configure these MCP servers in Claude Code:

1. **Datadog MCP** (required)
   - Run `/mcp` → Add Datadog server with API credentials

2. **Slack MCP** (optional, for auto-fetching alerts)
   - Run `/mcp` → Add Slack server

## Installation

```bash
/plugin install equal-ai-toolkit@claude-skills
```

## First-Run Setup

```bash
/investigate-alert
```

You'll be asked three questions:
- **AWS CLI profile name** (e.g., `ai-prod-read`)
- **Database username** (e.g., `equalreadonly`)
- **Database password**

All Equal AI infrastructure values (ECS cluster, services, database hosts) are pre-configured.

> **Note**: The config file contains credentials. Add `.claude/config/toolkit-config.yaml` to your `.gitignore`.

## Usage

### SRE Investigation

```bash
# Latest alert from Slack
/investigate-alert

# Specific issue
/investigate-alert "5xx on user-services"

# Datadog monitor ID
/investigate-alert 12345678
```

### Integration Tests

Mention "integration tests" in conversation:
- "Help me create integration tests for memory-service"
- "Add a test for the call processing endpoint"

## What's Included

### `/investigate-alert`

4-phase investigation: Gather → Investigate → Validate → Report

- Parallel subagents for infrastructure (ALB, ECS, RDS, Redis) and application (logs, traces, code)
- Evidence-based RCA with deep links
- Automatic service detection from Equal AI config

### Integration Test Expert

- Production-informed test coverage via Datadog
- Real database/Redis testing patterns
- pytest fixtures and factories

## Configuration

After setup, config is at `.claude/config/toolkit-config.yaml`:

```yaml
# User-specific (collected during setup)
aws:
  profile: your-profile
  region: ap-south-1

database_credentials:
  username: your-db-username
  password: your-db-password

# Pre-configured for Equal AI
infrastructure:
  ecs_cluster: EAI00EqualAiServicesCluster00prod
  alb_name: EAI00equalai-shared-alb00prod

services:
  - name: ai-backend
    # ...
```

**Important**: This file contains credentials. Ensure it's in `.gitignore`.

## Updating

When the plugin is updated:

```bash
cd ~/claude-skills && git pull
# Reinstall if needed
```

Your config file is preserved.
