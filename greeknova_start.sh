#!/bin/bash
LOG_FILE="/Users/apple/optionspulse/logs/greeknova.log"
PID_FILE="/Users/apple/optionspulse/logs/greeknova.pid"
mkdir -p /Users/apple/optionspulse/logs
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >> "$LOG_FILE"
echo "🚀 GreekNova starting at $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
DAY=$(date +%u)
if [ "$DAY" -ge 6 ]; then
    echo "⏭️  Weekend — skipping startup" >> "$LOG_FILE"
    exit 0
fi
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID"
        sleep 2
    fi
fi
cd /Users/apple/optionspulse
source venv/bin/activate
nohup python3 -m backend.main >> "$LOG_FILE" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$PID_FILE"
echo "✅ Backend started with PID $BACKEND_PID" >> "$LOG_FILE"
