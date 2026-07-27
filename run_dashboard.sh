#!/usr/bin/env bash
# Restart the Kalshi dashboard cleanly: kill any stale process on port 8080,
# start a fresh one, confirm it's up. Solves the "fixed the code but still
# seeing old numbers" problem — guarantees you're looking at current data.
set -e

PORT=8080

echo "Checking for a stale dashboard on port $PORT..."
STALE=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [ -n "$STALE" ]; then
    echo "  Killing stale process(es): $STALE"
    kill -9 $STALE 2>/dev/null || true
    sleep 1
else
    echo "  Port $PORT is free."
fi

echo "Starting fresh dashboard..."
# Run in background, log to a file, capture PID
nohup python3 dashboard.py > dashboard.log 2>&1 &
NEWPID=$!
echo "  Started dashboard (PID $NEWPID), logging to dashboard.log"

# Give Flask a moment, then confirm it's actually serving
sleep 2
if lsof -ti tcp:$PORT >/dev/null 2>&1; then
    echo ""
    echo "✅ Dashboard is up → http://localhost:$PORT"
    echo "   Reload that page to see current numbers."
    echo "   To stop it later:  kill $NEWPID   (or: lsof -ti tcp:$PORT | xargs kill)"
else
    echo ""
    echo "❌ Dashboard didn't come up. Check dashboard.log for the error:"
    tail -20 dashboard.log
fi
