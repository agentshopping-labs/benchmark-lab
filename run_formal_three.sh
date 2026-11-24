#!/bin/bash
# Formal three-round benchmark driver. Runs r1 -> r2 -> r3 sequentially.
set -u
LAB="/Users/internation/WorkBuddy AI/AgentShopping/outputs/benchmark-lab"
cd "$LAB" || exit 1
LOG="$LAB/runs/formal-three-rounds-$(date -u +%Y%m%d%H%M%S).log"
echo "=== driver start $(date -u +%FT%TZ) ===" >> "$LOG"
for r in r1 r2 r3; do
  echo "--- round $r begin $(date -u +%FT%TZ) ---" >> "$LOG"
  python3 runner/bench.py --round "$r" \
    --providers firecrawl,zenrows,brightdata \
    --delay 2 --max-errors 3 >> "$LOG" 2>&1
  rc=$?
  echo "--- round $r exit=$rc $(date -u +%FT%TZ) ---" >> "$LOG"
  if [ "$rc" -ne 0 ]; then
    echo "=== driver aborted on round $r failure ===" >> "$LOG"
    exit "$rc"
  fi
done
echo "=== driver done $(date -u +%FT%TZ) ===" >> "$LOG"
