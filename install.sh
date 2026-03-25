#!/bin/bash
# Install Equal AI Toolkit to ~/.claude/

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

echo "Installing Equal AI Toolkit..."

# Copy command
echo "  → Copying investigate-alert command..."
cp "$SCRIPT_DIR/plugins/equal-ai-toolkit/commands/investigate-alert.md" "$CLAUDE_DIR/commands/"

# Copy skill
echo "  → Copying integration-test-expert skill..."
mkdir -p "$CLAUDE_DIR/skills/integration-test-expert/references"
cp "$SCRIPT_DIR/plugins/equal-ai-toolkit/skills/integration-test-expert/SKILL.md" "$CLAUDE_DIR/skills/integration-test-expert/"
cp "$SCRIPT_DIR/plugins/equal-ai-toolkit/skills/integration-test-expert/references/"*.md "$CLAUDE_DIR/skills/integration-test-expert/references/"

echo ""
echo "✓ Installation complete!"
echo ""
echo "Usage:"
echo "  /investigate-alert          - Run SRE investigation (first run triggers setup)"
echo "  Mention 'integration test'  - Triggers integration test expert"
echo ""
echo "Configuration will be saved to your project's .claude/config/toolkit-config.yaml"
