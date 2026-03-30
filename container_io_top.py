#!/usr/bin/env python3
"""Container and device disk IO monitor.

- container mode: per-container read/write throughput from /proc/<pid>/io
- device mode: per-device iostat-like metrics from /proc/diskstats
- full mode: print both views in one sampling window
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
SYS_BLOCK = "/sys/block"


@dataclass
class IoStats:
    read_bytes: int
    write_bytes: int


@dataclass
class ContainerStats:
    name: str
    read_rate: float
    write_rate: float


@dataclass
class DiskStats:
    reads_completed: int
    reads_merged: int
    sectors_read: int
    read_ms: int
    writes_completed: int
    writes_merged: int
    sectors_written: int
    write_ms: int
    in_flight: int
    io_ms: int
    weighted_io_ms: int


@dataclass
class DeviceRates:
    device: str
    rps: float
    wps: float
    read_bps: float
    write_bps: float
    util_pct: float
    await_ms: float
    avgqu_sz: float
    avg_req_kb: float
    merge_pct: float
    pattern: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor container and device disk IO over a sampling interval",
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
        help="Show top N rows sorted by total IO (default: 20)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include rows with 0 activity",
    )
    parser.add_argument(
        "--no-resolve-name",
        action="store_true",
        help="Do not resolve container id to container name via docker ps",
    )
    parser.add_argument(
        "--mode",
        choices=("container", "device", "full"),
        default="container",
        help="container: per-container throughput, device: iostat-like, full: both",
    )
    parser.add_argument(
        "--include-loop",
        action="store_true",
        help="Include loop/ram devices in device mode",
    )
    parser.add_argument(
        "--device-regex",
        default="",
        help="Only include device names matching regex (device/full mode)",
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

        for full_id, cname in id_to_name.items():
            if full_id.startswith(cid) or cid.startswith(full_id):
                resolved[cid] = cname
                break

    return resolved


def get_block_devices(include_loop: bool) -> set[str]:
    devices: set[str] = set()
    try:
        for entry in os.scandir(SYS_BLOCK):
            name = entry.name
            if not include_loop and (name.startswith("loop") or name.startswith("ram")):
                continue
            devices.add(name)
    except OSError:
        pass
    return devices


def logical_block_size(device: str) -> int:
    path = os.path.join(SYS_BLOCK, device, "queue", "logical_block_size")
    content = read_file(path)
    if not content:
        return 512
    try:
        size = int(content.strip())
    except ValueError:
        return 512
    return size if size > 0 else 512


def snapshot_diskstats(include_loop: bool, device_re: Optional[re.Pattern[str]]) -> Dict[str, DiskStats]:
    data = read_file("/proc/diskstats")
    if not data:
        return {}

    block_devs = get_block_devices(include_loop)
    snap: Dict[str, DiskStats] = {}

    for line in data.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue

        name = parts[2]
        if name not in block_devs:
            continue
        if device_re and not device_re.search(name):
            continue

        try:
            vals = [int(x) for x in parts[3:14]]
        except ValueError:
            continue

        snap[name] = DiskStats(
            reads_completed=vals[0],
            reads_merged=vals[1],
            sectors_read=vals[2],
            read_ms=vals[3],
            writes_completed=vals[4],
            writes_merged=vals[5],
            sectors_written=vals[6],
            write_ms=vals[7],
            in_flight=vals[8],
            io_ms=vals[9],
            weighted_io_ms=vals[10],
        )

    return snap


def human_rate(num_bytes_per_sec: float) -> str:
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"]
    value = float(num_bytes_per_sec)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:8.1f} {unit}"
        value /= 1024.0
    return f"{value:8.1f} TiB/s"


def compute_container_rates(
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


def classify_pattern(avg_req_kb: float, merge_pct: float) -> str:
    if avg_req_kb >= 128.0 or merge_pct >= 50.0:
        return "LIKELY_SEQ"
    if avg_req_kb <= 32.0 and merge_pct <= 10.0:
        return "LIKELY_RANDOM"
    return "MIXED"


def compute_device_rates(
    start: Dict[str, DiskStats],
    end: Dict[str, DiskStats],
    seconds: float,
    include_zero: bool,
) -> list[DeviceRates]:
    rows: list[DeviceRates] = []
    devices = sorted(set(start.keys()) | set(end.keys()))

    for dev in devices:
        s = start.get(dev)
        e = end.get(dev)
        if not s or not e:
            continue

        d_reads = max(0, e.reads_completed - s.reads_completed)
        d_rmerge = max(0, e.reads_merged - s.reads_merged)
        d_rsectors = max(0, e.sectors_read - s.sectors_read)
        d_rms = max(0, e.read_ms - s.read_ms)

        d_writes = max(0, e.writes_completed - s.writes_completed)
        d_wmerge = max(0, e.writes_merged - s.writes_merged)
        d_wsectors = max(0, e.sectors_written - s.sectors_written)
        d_wms = max(0, e.write_ms - s.write_ms)

        d_io_ms = max(0, e.io_ms - s.io_ms)
        d_wio_ms = max(0, e.weighted_io_ms - s.weighted_io_ms)

        total_ios = d_reads + d_writes
        total_sectors = d_rsectors + d_wsectors
        total_merges = d_rmerge + d_wmerge

        blk = logical_block_size(dev)
        total_bytes = total_sectors * blk
        read_bps = (d_rsectors * blk) / seconds
        write_bps = (d_wsectors * blk) / seconds

        if not include_zero and total_bytes <= 0:
            continue

        util_pct = min(100.0, (d_io_ms / (seconds * 1000.0)) * 100.0)
        await_ms = ((d_rms + d_wms) / total_ios) if total_ios > 0 else 0.0
        avgqu_sz = d_wio_ms / (seconds * 1000.0)
        avg_req_kb = ((total_sectors * blk) / 1024.0 / total_ios) if total_ios > 0 else 0.0

        merge_base = total_ios + total_merges
        merge_pct = (total_merges / merge_base * 100.0) if merge_base > 0 else 0.0
        pattern = "IDLE" if total_ios == 0 else classify_pattern(avg_req_kb, merge_pct)

        rows.append(
            DeviceRates(
                device=dev,
                rps=d_reads / seconds,
                wps=d_writes / seconds,
                read_bps=read_bps,
                write_bps=write_bps,
                util_pct=util_pct,
                await_ms=await_ms,
                avgqu_sz=avgqu_sz,
                avg_req_kb=avg_req_kb,
                merge_pct=merge_pct,
                pattern=pattern,
            )
        )

    rows.sort(key=lambda x: x.read_bps + x.write_bps, reverse=True)
    return rows


def print_container_table(rows: list[ContainerStats], top_n: int, interval: float) -> None:
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


def print_device_table(rows: list[DeviceRates], top_n: int, interval: float) -> None:
    print(f"Device IO over {interval:.1f}s")
    print(
        f"{'DEVICE':<10} {'RPS':>7} {'WPS':>7} {'READ':>12} {'WRITE':>12} {'%UTIL':>7} {'AWAIT':>8} {'AVGQ':>7} {'REQ_KB':>8} {'MERGE%':>8} {'PATTERN':>15}"
    )
    print("-" * 122)

    if not rows:
        print("(no device IO activity detected)")
        return

    for row in rows[:top_n]:
        print(
            f"{row.device:<10.10} {row.rps:7.1f} {row.wps:7.1f} {human_rate(row.read_bps):>12} {human_rate(row.write_bps):>12} "
            f"{row.util_pct:7.1f} {row.await_ms:8.2f} {row.avgqu_sz:7.2f} {row.avg_req_kb:8.1f} {row.merge_pct:8.1f} {row.pattern:>15}"
        )


def main() -> int:
    args = parse_args()

    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2
    if args.top <= 0:
        print("--top must be > 0", file=sys.stderr)
        return 2

    device_re: Optional[re.Pattern[str]] = None
    if args.device_regex:
        try:
            device_re = re.compile(args.device_regex)
        except re.error as exc:
            print(f"Invalid --device-regex: {exc}", file=sys.stderr)
            return 2

    print(
        f"Sampling {args.mode} IO for {args.interval:.1f}s... (press Ctrl+C to stop)",
        flush=True,
    )

    c_start: Dict[str, IoStats] = {}
    c_end: Dict[str, IoStats] = {}
    d_start: Dict[str, DiskStats] = {}
    d_end: Dict[str, DiskStats] = {}

    if args.mode in ("container", "full"):
        c_start = snapshot_container_totals()
    if args.mode in ("device", "full"):
        d_start = snapshot_diskstats(args.include_loop, device_re)

    try:
        time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrupted before sampling window completed.", file=sys.stderr)
        return 130

    if args.mode in ("container", "full"):
        c_end = snapshot_container_totals()
    if args.mode in ("device", "full"):
        d_end = snapshot_diskstats(args.include_loop, device_re)

    if args.mode in ("container", "full"):
        all_ids = set(c_start.keys()) | set(c_end.keys())
        id_to_name = {} if args.no_resolve_name else resolve_container_names(all_ids)
        c_rows = compute_container_rates(
            start=c_start,
            end=c_end,
            seconds=args.interval,
            include_zero=args.all,
            id_to_name=id_to_name,
        )
        print_container_table(c_rows, top_n=args.top, interval=args.interval)

    if args.mode == "full":
        print()

    if args.mode in ("device", "full"):
        d_rows = compute_device_rates(
            start=d_start,
            end=d_end,
            seconds=args.interval,
            include_zero=args.all,
        )
        print_device_table(d_rows, top_n=args.top, interval=args.interval)

        print()
        print("Pattern note: LIKELY_SEQ/LIKELY_RANDOM is heuristic from req size + merge ratio.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
