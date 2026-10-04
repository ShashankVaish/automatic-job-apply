#!/usr/bin/env bash
# Launch Chrome with remote debugging so the bot can attach over CDP.
# Uses a dedicated profile (./chrome_bot_profile) so your normal Chrome
# windows and cookies are untouched. Log in to the job sites in THIS window.
set -euo pipefail

PORT=9222
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="$DIR/chrome_bot_profile"
mkdir -p "$PROFILE"

CANDIDATES=(
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  "/Applications/Chromium.app/Contents/MacOS/Chromium"
  "$(command -v google-chrome || true)"
  "$(command -v google-chrome-stable || true)"
  "$(command -v chromium || true)"
  "$(command -v chromium-browser || true)"
)

CHROME=""
for c in "${CANDIDATES[@]}"; do
  if [ -n "$c" ] && [ -x "$c" ]; then CHROME="$c"; break; fi
done

if [ -z "$CHROME" ]; then
  echo "Could not find Chrome. Install it, or edit CHROME in this script." >&2
  exit 1
fi

echo "Starting Chrome with debugging port $PORT"
echo "Profile: $PROFILE"
echo "Leave this Chrome window open while the bot runs."
"$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check &
echo "Chrome PID $!"
