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

# Resolve python3 from PATH at each start, so a Homebrew upgrade is picked up
# without reinstalling. PYTHONUNBUFFERED makes the log file show lines while
# the process stays up.
PY_DIR="$(cd "$(dirname "$PY")" && pwd)"
PATH_VALUE="$PY_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
xml_escape() {
  local s="$1"
  s=${s//&/&amp;}
  s=${s//</&lt;}
  s=${s//>/&gt;}
  printf '%s' "$s"
}

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$(xml_escape "$DIR/server.py")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PORT</key><string>$(xml_escape "$PORT")</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>$(xml_escape "$PATH_VALUE")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(xml_escape "$DIR/logs/launchd.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$DIR/logs/launchd.log")</string>
</dict>
</plist>
EOF

# Reload if it was already installed, then start it.
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed. The tracker is running at http://localhost:$PORT"
echo "Logs: $DIR/logs/server.log (crashes, if any: $DIR/logs/launchd.log)"
echo "To remove it later: ./uninstall-mac.sh"
