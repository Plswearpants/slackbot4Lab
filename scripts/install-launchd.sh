#!/usr/bin/env bash
# Install slackqa as a launchd user agent: starts at login, restarts if it dies.
#
# Run this yourself — it registers a standing background service on your Mac.
#
#   ./scripts/install-launchd.sh          install and start
#   ./scripts/install-launchd.sh remove   stop and unregister
#
# Calls the virtualenv's console script directly rather than going through uv.
# launchd runs with a minimal PATH where `uv` is not found, and skipping it also
# avoids a dependency re-sync on every restart. The venv install is editable, so
# code changes are still picked up — only a restart is needed to apply them.
set -euo pipefail

LABEL="com.lairbot.slackqa"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/slackqa}/bin/slackqa"
LOG="$HOME/Library/Logs/slackqa.log"

if [ "${1:-install}" = "remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL. The listener is stopped and will not start at login."
  exit 0
fi

[ -x "$BIN" ] || { echo "No slackqa executable at $BIN — run 'uv sync' first." >&2; exit 1; }
[ -f "$PROJECT/.env" ] || { echo "No .env at $PROJECT — copy .env.example and fill it in." >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>run</string>
  </array>

  <!-- .env, data/ and skills/ are all resolved relative to this. -->
  <key>WorkingDirectory</key><string>$PROJECT</string>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>

  <!-- A rejected API key exits immediately. Without a throttle launchd would
       spin on it; 60s keeps the retry loop visible in the log but harmless. -->
  <key>ThrottleInterval</key><integer>60</integer>

  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>

  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

echo "Installed $LABEL"
echo "  project : $PROJECT"
echo "  log     : $LOG"
echo
echo "It is running now and will start at login."
echo "  status : ./slackqa status      (or http://127.0.0.1:8765)"
echo "  logs   : tail -f $LOG"
echo "  restart: launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "  remove : ./scripts/install-launchd.sh remove"
