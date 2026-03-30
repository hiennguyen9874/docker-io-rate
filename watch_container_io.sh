#!/usr/bin/env bash
set -euo pipefail

# Realtime container + device disk IO monitor.

INTERVAL="${INTERVAL:-30}"
TOP="${TOP:-20}"
INCLUDE_ZERO="${INCLUDE_ZERO:-0}"
RESOLVE_NAME="${RESOLVE_NAME:-1}"
MODE="${MODE:-container}"
INCLUDE_LOOP="${INCLUDE_LOOP:-0}"
DEVICE_REGEX="${DEVICE_REGEX:-}"
SMART="${SMART:-0}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: watch_container_io.sh [options]

Options:
  -i, --interval SEC     Sampling interval in seconds (default: 30)
  -t, --top N            Show top N rows (default: 20)
  -m, --mode MODE        container | cgroup | device | full | health (default: container)
  -a, --all              Include rows with 0 activity
  --no-resolve-name      Show container ID instead of docker name
  --include-loop         Include loop/ram devices (device/full/health mode)
  --device-regex REGEX   Filter device name by regex (device/full/health mode)
  --smart                Query SMART health (health mode)
  -h, --help             Show this help

Environment variables (alternative config):
  INTERVAL, TOP, MODE, INCLUDE_ZERO (0/1), RESOLVE_NAME (0/1),
  INCLUDE_LOOP (0/1), DEVICE_REGEX, SMART (0/1), PYTHON_BIN

Example:
  sudo ./watch_container_io.sh --mode health --interval 5 --top 15
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
    -m|--mode)
      MODE="$2"
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
    --include-loop)
      INCLUDE_LOOP=1
      shift
      ;;
    --device-regex)
      DEVICE_REGEX="$2"
      shift 2
      ;;
    --smart)
      SMART=1
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

if [[ "$MODE" != "container" && "$MODE" != "cgroup" && "$MODE" != "device" && "$MODE" != "full" && "$MODE" != "health" ]]; then
  echo "Invalid mode: $MODE (must be container|cgroup|device|full|health)" >&2
  exit 2
fi

EXTRA_ARGS=(--mode "$MODE")
if [[ "$INCLUDE_ZERO" == "1" ]]; then
  EXTRA_ARGS+=(--all)
fi
if [[ "$RESOLVE_NAME" != "1" ]]; then
  EXTRA_ARGS+=(--no-resolve-name)
fi
if [[ "$INCLUDE_LOOP" == "1" ]]; then
  EXTRA_ARGS+=(--include-loop)
fi
if [[ -n "$DEVICE_REGEX" ]]; then
  EXTRA_ARGS+=(--device-regex "$DEVICE_REGEX")
fi
if [[ "$SMART" == "1" ]]; then
  EXTRA_ARGS+=(--smart)
fi

while true; do
  clear
  date '+%Y-%m-%d %H:%M:%S %Z'
  echo
  "$PYTHON_BIN" "$SCRIPT_DIR/container_io_top.py" \
    --interval "$INTERVAL" \
    --top "$TOP" \
    "${EXTRA_ARGS[@]}"
  echo
  echo "Press Ctrl+C to stop"
done
