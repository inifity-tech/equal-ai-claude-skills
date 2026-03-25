#!/bin/bash
# Uninstall Equal AI Toolkit from ~/.claude/

CLAUDE_DIR="$HOME/.claude"

echo "Uninstalling Equal AI Toolkit..."

rm -f "$CLAUDE_DIR/commands/investigate-alert.md"
rm -rf "$CLAUDE_DIR/skills/integration-test-expert"

echo "✓ Uninstalled"
