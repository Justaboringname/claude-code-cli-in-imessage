#!/usr/bin/env bash
# Install the iMessage Claude bot as a LaunchAgent.
# Idempotent: safe to re-run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_PATH="$REPO_DIR/bot.py"
LOG_DIR="$HOME/.config/imessage-claude-bot"
PLIST_TEMPLATE="$REPO_DIR/com.user.imessage-claude-bot.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.user.imessage-claude-bot.plist"
LABEL="com.user.imessage-claude-bot"

if [[ ! -f "$BOT_PATH" ]]; then
    echo "error: bot.py not found at $BOT_PATH" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$PLIST_TARGET")"

sed -e "s|__BOT_PATH__|$BOT_PATH|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    "$PLIST_TEMPLATE" > "$PLIST_TARGET"

if launchctl list | grep -q "$LABEL"; then
    echo "unloading existing agent..."
    launchctl unload "$PLIST_TARGET" 2>/dev/null || true
fi

launchctl load "$PLIST_TARGET"

echo
echo "installed: $PLIST_TARGET"
echo "logs:      $LOG_DIR/{bot,stdout,stderr}.log"
echo
echo "verify with:  launchctl list | grep imessage-claude-bot"
echo "tail logs:    tail -f $LOG_DIR/bot.log"
echo "uninstall:    ./uninstall.sh"
