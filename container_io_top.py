#!/usr/bin/env python3
"""Container and device disk IO monitor.

Modes:
- container: per-container read/write throughput from /proc/<pid>/io
- cgroup: per-container cgroup v2 io.stat throughput + IOPS
- device: per-device iostat-like metrics from /proc/diskstats
- full: print container + cgroup + device in one sampling window
- health: device metrics + host pressure/swap/fs saturation + alerts
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

CONTAINER_ID_RE = re.compile(r"([0-9a-f]{64}|[0-9a-f]{32})")
PROC_DIR = "/proc"
SYS_BLOCK = "/sys/block"
CGROUP_ROOT = "/sys/fs/cgroup"


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
class CgroupIoStat:
    rios: int
    wios: int
    rbytes: int
    wbytes: int


@dataclass
class CgroupContainerStats:
    name: str
    rios_rate: float
    wios_rate: float
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


@dataclass
class PressureTotals:
    some_us: int
    full_us: int


@dataclass
class HealthSnapshot:
    pressure: PressureTotals
    pswpin: int
    pswpout: int
    dirty_bytes: int
    writeback_bytes: int


@dataclass
class SmartHealth:
    device: str
    status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor container/device disk IO and host disk pressure",
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
        choices=("container", "cgroup", "device", "full", "health"),
        default="container",
        help="container|cgroup|device|full|health",
    )
    parser.add_argument(
        "--include-loop",
        action="store_true",
        help="Include loop/ram devices in device/full/health mode",
    )
    parser.add_argument(
        "--device-regex",
        default="",
        help="Only include device names matching regex (device/full/health mode)",
    )
    parser.add_argument(
        "--smart",
        action="store_true",
        help="In health mode, query SMART overall health (requires smartctl)",
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


def parse_cgroup_v2_path(content: str) -> Optional[str]:
    for line in content.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            path = parts[2].strip()
            if path.startswith("/"):
                return path
    return None


def pid_container_info_map() -> Dict[int, Tuple[str, Optional[str]]]:
    result: Dict[int, Tuple[str, Optional[str]]] = {}
    for entry in os.scandir(PROC_DIR):
        if not entry.name.isdigit():
            continue

        pid = int(entry.name)
        cgroup_path = os.path.join(PROC_DIR, entry.name, "cgroup")
        content = read_file(cgroup_path)
        if not content:
            continue

        cid = parse_container_id_from_cgroup(content)
        if not cid:
            continue

        cg_v2_path = parse_cgroup_v2_path(content)
        result[pid] = (cid, cg_v2_path)

    return result


def pid_container_map() -> Dict[int, str]:
    info = pid_container_info_map()
    return {pid: item[0] for pid, item in info.items()}


def container_cgroup_map() -> Dict[str, str]:
    cid_to_path: Dict[str, str] = {}
    for _pid, (cid, cgpath) in pid_container_info_map().items():
        if not cgpath:
            continue
        prev = cid_to_path.get(cid)
        if prev is None or len(cgpath) > len(prev):
            cid_to_path[cid] = cgpath
    return cid_to_path


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


def parse_cgroup_io_stat(content: str) -> CgroupIoStat:
    rios = wios = rbytes = wbytes = 0
    for line in content.splitlines():
        # Example: "8:0 rbytes=123 wbytes=456 rios=7 wios=8 dbytes=0 dios=0"
        for token in line.split()[1:]:
            if "=" not in token:
                continue
            k, v = token.split("=", 1)
            try:
                num = int(v)
            except ValueError:
                continue
            if k == "rios":
                rios += num
            elif k == "wios":
                wios += num
            elif k == "rbytes":
                rbytes += num
            elif k == "wbytes":
                wbytes += num
    return CgroupIoStat(rios=rios, wios=wios, rbytes=rbytes, wbytes=wbytes)


def snapshot_cgroup_container_totals() -> Dict[str, CgroupIoStat]:
    snap: Dict[str, CgroupIoStat] = {}
    for cid, cgpath in container_cgroup_map().items():
        io_stat_path = os.path.join(CGROUP_ROOT, cgpath.lstrip("/"), "io.stat")
        content = read_file(io_stat_path)
        if not content:
            continue
        snap[cid] = parse_cgroup_io_stat(content)
    return snap


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


def parse_pressure_io() -> PressureTotals:
    content = read_file("/proc/pressure/io") or ""
    some_total = 0
    full_total = 0
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        kind = parts[0]
        total_val = 0
        for token in parts[1:]:
            if token.startswith("total="):
                try:
                    total_val = int(token.split("=", 1)[1])
                except ValueError:
                    total_val = 0
                break
        if kind == "some":
            some_total = total_val
        elif kind == "full":
            full_total = total_val
    return PressureTotals(some_us=some_total, full_us=full_total)


def parse_vmstat(keys: Iterable[str]) -> Dict[str, int]:
    wanted = set(keys)
    out: Dict[str, int] = {k: 0 for k in wanted}
    content = read_file("/proc/vmstat") or ""
    for line in content.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        k, v = parts
        if k not in wanted:
            continue
        try:
            out[k] = int(v)
        except ValueError:
            out[k] = 0
    return out


def parse_meminfo() -> Tuple[int, int]:
    dirty_kb = 0
    writeback_kb = 0
    content = read_file("/proc/meminfo") or ""
    for line in content.splitlines():
        if line.startswith("Dirty:"):
            try:
                dirty_kb = int(line.split()[1])
            except (ValueError, IndexError):
                dirty_kb = 0
        elif line.startswith("Writeback:"):
            try:
                writeback_kb = int(line.split()[1])
            except (ValueError, IndexError):
                writeback_kb = 0
    return dirty_kb * 1024, writeback_kb * 1024


def snapshot_health() -> HealthSnapshot:
    vm = parse_vmstat(["pswpin", "pswpout"])
    dirty_bytes, writeback_bytes = parse_meminfo()
    return HealthSnapshot(
        pressure=parse_pressure_io(),
        pswpin=vm.get("pswpin", 0),
        pswpout=vm.get("pswpout", 0),
        dirty_bytes=dirty_bytes,
        writeback_bytes=writeback_bytes,
    )


def parse_df_percent(args: list[str]) -> list[Tuple[str, str, int]]:
    # returns [(filesystem, mountpoint, used_percent)]
    try:
        output = subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    rows: list[Tuple[str, str, int]] = []
    lines = output.splitlines()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        fs = parts[0]
        used_pct_raw = parts[4]
        mount = parts[5]
        try:
            used_pct = int(used_pct_raw.rstrip("%"))
        except ValueError:
            continue
        rows.append((fs, mount, used_pct))
    return rows


def list_network_mounts() -> list[Tuple[str, str, str]]:
    mounts: list[Tuple[str, str, str]] = []
    content = read_file("/proc/mounts") or ""
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mountpoint, fstype = parts[0], parts[1], parts[2]
        if fstype.startswith("nfs") or "ceph" in fstype:
            mounts.append((source, mountpoint, fstype))
    return mounts


def collect_smart_health(devices: Iterable[str]) -> list[SmartHealth]:
    if shutil.which("smartctl") is None:
        return []

    out: list[SmartHealth] = []
    for dev in devices:
        path = f"/dev/{dev}"
        try:
            text = subprocess.check_output(
                ["smartctl", "-H", path],
                stderr=subprocess.STDOUT,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

        status = "UNKNOWN"
        for line in text.splitlines():
            u = line.upper()
            if "PASSED" in u or "OK" in u:
                status = "PASSED"
                break
            if "FAILED" in u or "BAD" in u:
                status = "FAILED"
                break
        out.append(SmartHealth(device=dev, status=status))

    return out


def human_rate(num_bytes_per_sec: float) -> str:
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"]
    value = float(num_bytes_per_sec)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:8.1f} {unit}"
        value /= 1024.0
    return f"{value:8.1f} TiB/s"


def human_bytes(num_bytes: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(num_bytes)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:8.1f} {unit}"
        value /= 1024.0
    return f"{value:8.1f} TiB"


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


def compute_cgroup_container_rates(
    start: Dict[str, CgroupIoStat],
    end: Dict[str, CgroupIoStat],
    seconds: float,
    include_zero: bool,
    id_to_name: Dict[str, str],
) -> list[CgroupContainerStats]:
    containers = sorted(set(start.keys()) | set(end.keys()))
    rows: list[CgroupContainerStats] = []

    for cid in containers:
        s = start.get(cid, CgroupIoStat(0, 0, 0, 0))
        e = end.get(cid, CgroupIoStat(0, 0, 0, 0))

        rios = max(0, e.rios - s.rios)
        wios = max(0, e.wios - s.wios)
        rbytes = max(0, e.rbytes - s.rbytes)
        wbytes = max(0, e.wbytes - s.wbytes)

        rios_rate = rios / seconds
        wios_rate = wios / seconds
        read_rate = rbytes / seconds
        write_rate = wbytes / seconds

        if not include_zero and rios + wios + rbytes + wbytes <= 0:
            continue

        cname = id_to_name.get(cid, cid[:12])
        rows.append(
            CgroupContainerStats(
                name=cname,
                rios_rate=rios_rate,
                wios_rate=wios_rate,
                read_rate=read_rate,
                write_rate=write_rate,
            )
        )

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


def print_cgroup_table(rows: list[CgroupContainerStats], top_n: int, interval: float) -> None:
    print(f"Container cgroup io.stat over {interval:.1f}s")
    print(
        f"{'CONTAINER':<36} {'RIOS/s':>10} {'WIOS/s':>10} {'READ':>16} {'WRITE':>16} {'TOTAL':>16}"
    )
    print("-" * 108)

    if not rows:
        print("(no cgroup io.stat activity detected)")
        return

    for row in rows[:top_n]:
        total = row.read_rate + row.write_rate
        print(
            f"{row.name:<36.36} {row.rios_rate:10.1f} {row.wios_rate:10.1f} {human_rate(row.read_rate):>16} {human_rate(row.write_rate):>16} {human_rate(total):>16}"
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


def build_health_alerts(
    device_rows: list[DeviceRates],
    health_start: HealthSnapshot,
    health_end: HealthSnapshot,
    fs_usage: list[Tuple[str, str, int]],
    inode_usage: list[Tuple[str, str, int]],
    network_mounts: list[Tuple[str, str, str]],
    seconds: float,
    smart_health: list[SmartHealth],
) -> list[str]:
    alerts: list[str] = []

    util_thr = 90.0
    await_thr = 20.0
    avgq_thr = 2.0
    psi_thr_msps = 50.0
    swap_thr_pages = 10.0
    dirty_thr_bytes = 1024 * 1024 * 1024
    fs_thr = 90

    for row in device_rows:
        if row.util_pct >= util_thr:
            alerts.append(f"device {row.device}: high util {row.util_pct:.1f}%")
        if row.await_ms >= await_thr:
            alerts.append(f"device {row.device}: high await {row.await_ms:.2f} ms")
        if row.avgqu_sz >= avgq_thr:
            alerts.append(f"device {row.device}: high queue depth {row.avgqu_sz:.2f}")
        if row.pattern == "LIKELY_RANDOM" and (row.rps + row.wps) >= 100.0:
            alerts.append(f"device {row.device}: random small IO pressure (pattern={row.pattern}, iops={row.rps + row.wps:.1f})")

    d_some_us = max(0, health_end.pressure.some_us - health_start.pressure.some_us)
    d_full_us = max(0, health_end.pressure.full_us - health_start.pressure.full_us)
    some_msps = (d_some_us / 1000.0) / seconds
    full_msps = (d_full_us / 1000.0) / seconds
    if some_msps >= psi_thr_msps:
        alerts.append(f"IO PSI some is high: {some_msps:.1f} ms/s")
    if full_msps >= psi_thr_msps:
        alerts.append(f"IO PSI full is high: {full_msps:.1f} ms/s")

    swpin_ps = max(0, health_end.pswpin - health_start.pswpin) / seconds
    swpout_ps = max(0, health_end.pswpout - health_start.pswpout) / seconds
    if swpin_ps >= swap_thr_pages or swpout_ps >= swap_thr_pages:
        alerts.append(f"swap activity elevated: pswpin={swpin_ps:.1f}/s pswpout={swpout_ps:.1f}/s")

    if health_end.dirty_bytes >= dirty_thr_bytes:
        alerts.append(f"dirty memory is high: {human_bytes(health_end.dirty_bytes)}")
    if health_end.writeback_bytes >= dirty_thr_bytes:
        alerts.append(f"writeback memory is high: {human_bytes(health_end.writeback_bytes)}")

    for fs, mount, used in fs_usage:
        if used >= fs_thr:
            alerts.append(f"filesystem usage high: {mount} ({fs}) {used}%")

    for fs, mount, used in inode_usage:
        if used >= fs_thr:
            alerts.append(f"inode usage high: {mount} ({fs}) {used}%")

    if network_mounts:
        alerts.append("network storage mounts detected (NFS/Ceph); backend latency can dominate IO")

    for s in smart_health:
        if s.status == "FAILED":
            alerts.append(f"SMART health failed on {s.device}")

    return alerts


def print_health_section(
    health_start: HealthSnapshot,
    health_end: HealthSnapshot,
    fs_usage: list[Tuple[str, str, int]],
    inode_usage: list[Tuple[str, str, int]],
    network_mounts: list[Tuple[str, str, str]],
    smart_health: list[SmartHealth],
    seconds: float,
) -> None:
    d_some_us = max(0, health_end.pressure.some_us - health_start.pressure.some_us)
    d_full_us = max(0, health_end.pressure.full_us - health_start.pressure.full_us)
    some_msps = (d_some_us / 1000.0) / seconds
    full_msps = (d_full_us / 1000.0) / seconds

    swpin_ps = max(0, health_end.pswpin - health_start.pswpin) / seconds
    swpout_ps = max(0, health_end.pswpout - health_start.pswpout) / seconds

    print("Host IO Health")
    print("-" * 88)
    print(f"IO PSI some: {some_msps:.2f} ms/s")
    print(f"IO PSI full: {full_msps:.2f} ms/s")
    print(f"Swap activity: pswpin={swpin_ps:.2f}/s pswpout={swpout_ps:.2f}/s")
    print(f"Dirty memory: {human_bytes(health_end.dirty_bytes)}")
    print(f"Writeback memory: {human_bytes(health_end.writeback_bytes)}")

    if fs_usage:
        worst_fs = sorted(fs_usage, key=lambda x: x[2], reverse=True)[:3]
        print("Top filesystem usage:")
        for fs, mount, used in worst_fs:
            print(f"  {mount} ({fs}): {used}%")

    if inode_usage:
        worst_inode = sorted(inode_usage, key=lambda x: x[2], reverse=True)[:3]
        print("Top inode usage:")
        for fs, mount, used in worst_inode:
            print(f"  {mount} ({fs}): {used}%")

    if network_mounts:
        print("Network mounts (NFS/Ceph):")
        for src, mount, fstype in network_mounts[:5]:
            print(f"  {mount} <- {src} ({fstype})")

    if smart_health:
        print("SMART health:")
        for s in smart_health:
            print(f"  {s.device}: {s.status}")


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
    cg_start: Dict[str, CgroupIoStat] = {}
    cg_end: Dict[str, CgroupIoStat] = {}
    d_start: Dict[str, DiskStats] = {}
    d_end: Dict[str, DiskStats] = {}
    h_start: Optional[HealthSnapshot] = None
    h_end: Optional[HealthSnapshot] = None

    if args.mode in ("container", "full"):
        c_start = snapshot_container_totals()
    if args.mode in ("cgroup", "full"):
        cg_start = snapshot_cgroup_container_totals()
    if args.mode in ("device", "full", "health"):
        d_start = snapshot_diskstats(args.include_loop, device_re)
    if args.mode == "health":
        h_start = snapshot_health()

    try:
        time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrupted before sampling window completed.", file=sys.stderr)
        return 130

    if args.mode in ("container", "full"):
        c_end = snapshot_container_totals()
    if args.mode in ("cgroup", "full"):
        cg_end = snapshot_cgroup_container_totals()
    if args.mode in ("device", "full", "health"):
        d_end = snapshot_diskstats(args.include_loop, device_re)
    if args.mode == "health":
        h_end = snapshot_health()

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

    if args.mode in ("cgroup", "full"):
        all_ids = set(cg_start.keys()) | set(cg_end.keys())
        id_to_name = {} if args.no_resolve_name else resolve_container_names(all_ids)
        cg_rows = compute_cgroup_container_rates(
            start=cg_start,
            end=cg_end,
            seconds=args.interval,
            include_zero=args.all,
            id_to_name=id_to_name,
        )
        print_cgroup_table(cg_rows, top_n=args.top, interval=args.interval)

    if args.mode in ("full", "cgroup"):
        print()

    if args.mode in ("device", "full", "health"):
        d_rows = compute_device_rates(
            start=d_start,
            end=d_end,
            seconds=args.interval,
            include_zero=args.all,
        )
        print_device_table(d_rows, top_n=args.top, interval=args.interval)
        print()
        print("Pattern note: LIKELY_SEQ/LIKELY_RANDOM is heuristic from req size + merge ratio.")

    if args.mode == "health" and h_start and h_end:
        fs_usage = parse_df_percent(["df", "-P"])
        inode_usage = parse_df_percent(["df", "-Pi"])
        network_mounts = list_network_mounts()

        smart_health: list[SmartHealth] = []
        if args.smart:
            smart_health = collect_smart_health(d_end.keys())

        print()
        print_health_section(
            health_start=h_start,
            health_end=h_end,
            fs_usage=fs_usage,
            inode_usage=inode_usage,
            network_mounts=network_mounts,
            smart_health=smart_health,
            seconds=args.interval,
        )

        alerts = build_health_alerts(
            device_rows=d_rows,
            health_start=h_start,
            health_end=h_end,
            fs_usage=fs_usage,
            inode_usage=inode_usage,
            network_mounts=network_mounts,
            seconds=args.interval,
            smart_health=smart_health,
        )

        print()
        print("Health Alerts")
        print("-" * 88)
        if alerts:
            for item in alerts:
                print(f"- {item}")
        else:
            print("- no obvious pressure signals in this sampling window")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
