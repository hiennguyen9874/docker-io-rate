#!/usr/bin/env bash
set -euo pipefail

# Realtime container disk IO monitor.
# Default refresh every 30s (similar to repeated iotop snapshots).

INTERVAL="${INTERVAL:-30}"
TOP="${TOP:-20}"
INCLUDE_ZERO="${INCLUDE_ZERO:-0}"
RESOLVE_NAME="${RESOLVE_NAME:-1}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: watch_container_io.sh [options]

Options:
  -i, --interval SEC     Sampling interval in seconds (default: 30)
  -t, --top N            Show top N containers (default: 20)
  -a, --all              Include containers with 0 B/s IO
  --no-resolve-name      Show container ID instead of resolving docker names
  -h, --help             Show this help

Environment variables (alternative config):
  INTERVAL, TOP, INCLUDE_ZERO (0/1), RESOLVE_NAME (0/1), PYTHON_BIN

Example:
  sudo ./watch_container_io.sh --interval 15 --top 10
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--interval)
      INTERVAL="$2"
      shift 2
      ;;
    -t|--top)
      TOP="$2"
      shift 2
      ;;
    -a|--all)
      INCLUDE_ZERO=1
      shift
      ;;
    --no-resolve-name)
      RESOLVE_NAME=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python binary not found: $PYTHON_BIN" >&2
  exit 1
fi

EXTRA_ARGS=()
if [[ "$INCLUDE_ZERO" == "1" ]]; then
  EXTRA_ARGS+=(--all)
fi
if [[ "$RESOLVE_NAME" != "1" ]]; then
  EXTRA_ARGS+=(--no-resolve-name)
fi

while true; do
  clear
  echo "$(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo
  "$PYTHON_BIN" "$SCRIPT_DIR/container_io_top.py" \
    --interval "$INTERVAL" \
    --top "$TOP" \
    "${EXTRA_ARGS[@]}"
  echo
  echo "Press Ctrl+C to stop"
done
