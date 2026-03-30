#!/usr/bin/env python3
"""Show per-container disk IO rate similar to `iotop -oPa`.

Reads `/proc/<pid>/io`, aggregates by container id/name, and prints the
containers with highest read/write throughput over a sampling interval.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

CONTAINER_ID_RE = re.compile(r"([0-9a-f]{64}|[0-9a-f]{32})")
PROC_DIR = "/proc"


@dataclass
class IoStats:
    read_bytes: int
    write_bytes: int


@dataclass
class ContainerStats:
    name: str
    read_rate: float
    write_rate: float



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show container disk IO rate by sampling /proc/<pid>/io",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Sampling interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Show top N containers sorted by total IO (default: 20)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include containers with 0 B/s IO",
    )
    parser.add_argument(
        "--no-resolve-name",
        action="store_true",
        help="Do not resolve container id to container name via docker ps",
    )
    return parser.parse_args()



def read_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None



def parse_container_id_from_cgroup(content: str) -> Optional[str]:
    best: Optional[str] = None
    for line in content.splitlines():
        for match in CONTAINER_ID_RE.findall(line):
            if best is None or len(match) > len(best):
                best = match
    return best



def pid_container_map() -> Dict[int, str]:
    result: Dict[int, str] = {}
    for entry in os.scandir(PROC_DIR):
        if not entry.name.isdigit():
            continue

        pid = int(entry.name)
        cgroup_path = os.path.join(PROC_DIR, entry.name, "cgroup")
        content = read_file(cgroup_path)
        if not content:
            continue

        cid = parse_container_id_from_cgroup(content)
        if cid:
            result[pid] = cid

    return result



def parse_io_file(pid: int) -> Optional[IoStats]:
    io_path = os.path.join(PROC_DIR, str(pid), "io")
    content = read_file(io_path)
    if not content:
        return None

    read_bytes = 0
    write_bytes = 0
    for line in content.splitlines():
        if line.startswith("read_bytes:"):
            read_bytes = int(line.split(":", 1)[1].strip())
        elif line.startswith("write_bytes:"):
            write_bytes = int(line.split(":", 1)[1].strip())

    return IoStats(read_bytes=read_bytes, write_bytes=write_bytes)



def snapshot_container_totals() -> Dict[str, IoStats]:
    totals: Dict[str, Tuple[int, int]] = {}
    mapping = pid_container_map()

    for pid, cid in mapping.items():
        stats = parse_io_file(pid)
        if stats is None:
            continue

        prev = totals.get(cid)
        if prev is None:
            totals[cid] = (stats.read_bytes, stats.write_bytes)
        else:
            totals[cid] = (prev[0] + stats.read_bytes, prev[1] + stats.write_bytes)

    return {cid: IoStats(r, w) for cid, (r, w) in totals.items()}



def resolve_container_names(ids: Iterable[str]) -> Dict[str, str]:
    try:
        output = subprocess.check_output(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}

    id_to_name: Dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        full_id, name = parts[0], parts[1]
        id_to_name[full_id] = name

    resolved: Dict[str, str] = {}
    for cid in ids:
        name = id_to_name.get(cid)
        if name:
            resolved[cid] = name
            continue

        # Fallback for non-truncated IDs that may contain full sha with prefix.
        for full_id, cname in id_to_name.items():
            if full_id.startswith(cid) or cid.startswith(full_id):
                resolved[cid] = cname
                break

    return resolved



def human_rate(num_bytes_per_sec: float) -> str:
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"]
    value = float(num_bytes_per_sec)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:8.1f} {unit}"
        value /= 1024.0
    return f"{value:8.1f} TiB/s"



def compute_rates(
    start: Dict[str, IoStats],
    end: Dict[str, IoStats],
    seconds: float,
    include_zero: bool,
    id_to_name: Dict[str, str],
) -> list[ContainerStats]:
    containers = sorted(set(start.keys()) | set(end.keys()))
    rows: list[ContainerStats] = []

    for cid in containers:
        start_stat = start.get(cid, IoStats(0, 0))
        end_stat = end.get(cid, IoStats(0, 0))

        read_delta = max(0, end_stat.read_bytes - start_stat.read_bytes)
        write_delta = max(0, end_stat.write_bytes - start_stat.write_bytes)

        read_rate = read_delta / seconds
        write_rate = write_delta / seconds

        if not include_zero and read_rate <= 0 and write_rate <= 0:
            continue

        cname = id_to_name.get(cid, cid[:12])
        rows.append(ContainerStats(name=cname, read_rate=read_rate, write_rate=write_rate))

    rows.sort(key=lambda x: x.read_rate + x.write_rate, reverse=True)
    return rows



def print_table(rows: list[ContainerStats], top_n: int, interval: float) -> None:
    print(f"Container IO over {interval:.1f}s")
    print(f"{'CONTAINER':<36} {'READ':>16} {'WRITE':>16} {'TOTAL':>16}")
    print("-" * 88)

    if not rows:
        print("(no container IO activity detected)")
        return

    for row in rows[:top_n]:
        total = row.read_rate + row.write_rate
        print(
            f"{row.name:<36.36} {human_rate(row.read_rate):>16} {human_rate(row.write_rate):>16} {human_rate(total):>16}"
        )



def main() -> int:
    args = parse_args()

    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2
    if args.top <= 0:
        print("--top must be > 0", file=sys.stderr)
        return 2

    start = snapshot_container_totals()
    time.sleep(args.interval)
    end = snapshot_container_totals()

    all_ids = set(start.keys()) | set(end.keys())
    id_to_name = {} if args.no_resolve_name else resolve_container_names(all_ids)

    rows = compute_rates(
        start=start,
        end=end,
        seconds=args.interval,
        include_zero=args.all,
        id_to_name=id_to_name,
    )
    print_table(rows, top_n=args.top, interval=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
