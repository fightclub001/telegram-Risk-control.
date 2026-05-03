#!/bin/bash
# Mac health watcher for telegram-risk-control
# Run every 5 minutes via crontab or launchd timer

STATUS=$(curl -s --max-time 10 http://127.0.0.1:18080/status 2>/dev/null)
if [ -z "$STATUS" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] HEALTH DOWN: no response from localhost:18080"
    exit 1
fi

HEALTHY=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('healthy',False))" 2>/dev/null)
if [ "$HEALTHY" != "True" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] HEALTH WARN: healthy=$HEALTHY | $STATUS"
    exit 2
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] OK"
exit 0
