#!/usr/bin/env bash
set -euo pipefail
cd /root/.openclaw/workspace/projects/pra-rulebook-explorer
log=$(cat logs/bge-m3-context-3200-rebuild.latest)
state=logs/bge-m3-context-3200-monitor.state
last_progress=''
last_progress_ts=$(date +%s)
start_ts=$(date +%s)

echo "monitor_started=$(date -Is) log=$log" > "$state"

while true; do
  now=$(date +%s)
  proc_line=$(pgrep -af "backend.app.cli build-indexes.*BAAI/bge-m3.*text-chars 3200" || true)
  tail_text=$(tail -80 "$log" 2>/dev/null || true)
  progress=$(printf '%s\n' "$tail_text" | tr '\r' '\n' | grep 'Batches:' | tail -1 || true)

  if [[ -n "$progress" && "$progress" != "$last_progress" ]]; then
    last_progress="$progress"
    last_progress_ts=$now
  fi

  {
    echo "checked=$(date -Is)"
    echo "process=${proc_line:-none}"
    echo "last_progress=${last_progress:-none}"
    echo "last_progress_age_seconds=$((now-last_progress_ts))"
    echo "log=$log"
  } > "$state"

  if grep -q "=== done" "$log" 2>/dev/null; then
    echo "status=done checked=$(date -Is)" >> "$state"
    exit 0
  fi

  if grep -qE "Killed|Traceback|Error|Exception" "$log" 2>/dev/null && [[ -z "$proc_line" ]]; then
    echo "status=failed checked=$(date -Is)" >> "$state"
    tail -120 "$log" > logs/bge-m3-context-3200-monitor.failure-tail
    exit 2
  fi

  if [[ -z "$proc_line" ]]; then
    echo "status=process_missing checked=$(date -Is)" >> "$state"
    tail -120 "$log" > logs/bge-m3-context-3200-monitor.failure-tail
    exit 3
  fi

  if (( now - last_progress_ts > 5400 )); then
    echo "status=possible_stall checked=$(date -Is)" >> "$state"
    tail -120 "$log" > logs/bge-m3-context-3200-monitor.stall-tail
    exit 4
  fi

  # Expected to run for many hours. Keep checks sparse.
  sleep 1800

done
