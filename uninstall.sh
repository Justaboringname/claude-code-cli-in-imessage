#!/usr/bin/env bash
set -euo pipefail

PLIST_TARGET="$HOME/Library/LaunchAgents/com.user.imessage-claude-bot.plist"

if [[ -f "$PLIST_TARGET" ]]; then
    launchctl unload "$PLIST_TARGET" 2>/dev/null || true
    rm "$PLIST_TARGET"
    echo "removed $PLIST_TARGET"
else
    echo "nothing installed at $PLIST_TARGET"
fi

echo "(state and logs left in ~/.config/imessage-claude-bot — delete manually if desired)"
