#!/bin/bash
# Keeps the net worth price server running in the background on macOS.
# It starts at login and restarts itself if it crashes.
# Usage: ./install-mac.sh
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"
LABEL="com.networth.tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${PORT:-8787}"

if [ -z "$PY" ]; then
  echo "python3 was not found. Install it first (for example: xcode-select --install)."
  exit 1
fi
if [ ! -f "$DIR/server.py" ]; then
  echo "server.py not found next to this script."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PORT</key><string>$PORT</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/networth-tracker.log</string>
  <key>StandardErrorPath</key><string>/tmp/networth-tracker.log</string>
</dict>
</plist>
EOF

# Reload if it was already installed, then start it.
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed. The tracker is running at http://localhost:$PORT"
echo "Logs: /tmp/networth-tracker.log"
echo "To remove it later: ./uninstall-mac.sh"
