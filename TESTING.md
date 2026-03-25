# Testing the Plugin Repository

This guide explains how to test the plugin locally before pushing to GitHub.

## Prerequisites

- Claude Code CLI installed
- A project directory to test in (e.g., your main codebase)

## Testing Workflow

### Phase 1: Local Installation

Install the plugin directly from the local path:

```bash
# Navigate to your test project
cd /path/to/your/project

# Install from local path
/plugin install ~/claude-skills/plugins/equal-ai-toolkit
```

### Phase 2: Test First-Run Setup

1. **Ensure no existing config:**
   ```bash
   rm -f .claude/config/toolkit-config.yaml
   ```

2. **Run the command:**
   ```bash
   /investigate-alert
   ```

3. **Verify setup flow:**
   - [ ] Setup detection triggers (no config found)
   - [ ] AWS profile question appears
   - [ ] AWS region question appears
   - [ ] ECS cluster question appears
   - [ ] ALB name question appears
   - [ ] Slack channel question appears
   - [ ] Service mapping collection works
   - [ ] Database configuration questions appear
   - [ ] Redis configuration questions appear
   - [ ] Test environment configuration questions appear

4. **Verify config file created:**
   ```bash
   cat .claude/config/toolkit-config.yaml
   ```
   - [ ] All sections present (aws, infrastructure, slack, services, databases, redis, testing)
   - [ ] YAML is valid
   - [ ] Values match what you entered

5. **Verify setup section removed:**
   - [ ] The skill file no longer contains "FIRST-RUN SETUP SECTION"

### Phase 3: Test Subsequent Runs

#### SRE Investigation

```bash
# Test with a description
/investigate-alert "test alert - 5xx on api-gateway"
```

Verify:
- [ ] Config is loaded (no setup prompts)
- [ ] Alert Profile is built
- [ ] Investigation classification happens
- [ ] Subagents are launched (if Datadog MCP available)

#### Integration Test Expert

Mention "help me create integration tests for user-service" in conversation.

Verify:
- [ ] Config is loaded (no setup prompt, no redirect to /investigate-alert)
- [ ] Mode selection appears (Full Setup vs Add Tests)
- [ ] Service selection works (uses services from config)

### Phase 4: Test Config Not Found (Integration Test Expert)

1. **Delete config:**
   ```bash
   rm .claude/config/toolkit-config.yaml
   ```

2. **Trigger integration test skill** (mention "create integration tests")

3. **Verify redirect:**
   - [ ] Skill detects missing config
   - [ ] Tells user to run `/investigate-alert` for setup
   - [ ] Does NOT try to run its own setup

### Phase 5: Test Reset/Re-Setup

1. **Delete config:**
   ```bash
   rm .claude/config/toolkit-config.yaml
   ```

2. **Uninstall and reinstall:**
   ```bash
   /plugin uninstall equal-ai-toolkit
   /plugin install ~/claude-skills/plugins/equal-ai-toolkit
   ```

3. **Run setup:**
   ```bash
   /investigate-alert
   ```

4. **Verify setup triggers again**

## Quick Reset Script

```bash
#!/bin/bash
# reset-test.sh

echo "=== Resetting plugin test environment ==="

rm -f .claude/config/toolkit-config.yaml
echo "Config removed."

echo ""
echo "Now run in Claude Code:"
echo "  /plugin uninstall equal-ai-toolkit"
echo "  /plugin install ~/claude-skills/plugins/equal-ai-toolkit"
echo ""
echo "Then test:"
echo "  /investigate-alert"
```

## Test Checklist

### Setup Flow
- [ ] Detects missing config
- [ ] Asks AWS profile
- [ ] Asks AWS region
- [ ] Asks ECS cluster
- [ ] Asks ALB name
- [ ] Asks Slack channel
- [ ] Collects service mappings (at least 1)
- [ ] Asks database config
- [ ] Asks Redis config
- [ ] Asks test environment config
- [ ] Writes valid YAML
- [ ] Removes setup section from skill file

### SRE Investigation
- [ ] Loads config correctly
- [ ] Parses $ARGUMENTS
- [ ] Builds Alert Profile
- [ ] Classifies alert (APPLICATION/INFRASTRUCTURE)
- [ ] Launches subagents
- [ ] Produces RCA report

### Integration Test Expert
- [ ] Detects missing config → redirects to /investigate-alert
- [ ] Loads config when present
- [ ] Mode selection works
- [ ] Service selection uses config
- [ ] Datadog analysis works (if MCP available)
- [ ] Test generation follows patterns

## Troubleshooting

### Plugin Not Found
```
Error: Plugin not found at path
```
**Fix:** Use absolute path: `/plugin install ~/claude-skills/plugins/equal-ai-toolkit`

### Setup Section Not Removed
Check if markers exist exactly as specified in the skill file.

**Manual fix:** Edit the file to remove the setup section, then test the main functionality.

### Config Not Created
```bash
mkdir -p .claude/config
```

### Integration Test Expert Runs Own Setup
This is a bug - it should redirect to `/investigate-alert`. Check the SKILL.md file for correct config detection logic.
