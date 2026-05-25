#!/bin/bash
LOG_FILE="/Users/apple/optionspulse/logs/greeknova.log"
PID_FILE="/Users/apple/optionspulse/logs/greeknova.pid"
TOKEN_FILE="/Users/apple/.greeksnova_token"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >> "$LOG_FILE"
echo "🛑 GreekNova stopping at $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
DAY=$(date +%u)
if [ "$DAY" -ge 6 ]; then
    echo "⏭️  Weekend — nothing to stop" >> "$LOG_FILE"
    exit 0
fi
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "✅ Backend process $PID stopped" >> "$LOG_FILE"
    fi
    rm -f "$PID_FILE"
else
    pkill -f "backend.main" && echo "✅ Backend stopped via pkill" >> "$LOG_FILE"
fi
if [ -f "$TOKEN_FILE" ]; then
    rm -f "$TOKEN_FILE"
    echo "🗑️  Token cleared" >> "$LOG_FILE"
fi
echo "💤 Market closed. GreekNova offline till tomorrow 8:30 AM" >> "$LOG_FILE"
