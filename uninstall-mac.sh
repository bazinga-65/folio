#!/bin/bash
# Stops the background price server and removes it from login items.
LABEL="com.networth.tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed. The price server will no longer start automatically."
