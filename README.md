# Docker Container + Disk IO Monitor

Scripts để theo dõi nguyên nhân nghẽn disk IO theo nhiều lớp: container, cgroup, device và health signals của host.

## Files

- `container_io_top.py`
  - `container`: read/write throughput theo container từ `/proc/<pid>/io`
  - `cgroup`: read/write + IOPS theo container từ `cgroup v2 io.stat`
  - `device`: chỉ số kiểu `iostat -x` từ `/proc/diskstats`
  - `full`: in `container + cgroup + device`
  - `health`: `device` + IO PSI + swap + dirty/writeback + fs/inode usage + alerts
- `watch_container_io.sh`
  - realtime loop, refresh theo `interval`

## Requirements

- Linux host có `/proc`, `/sys/block`, `/sys/fs/cgroup`
- Python 3
- Docker CLI (nếu muốn resolve container name)
- Quyền đọc `/proc/<pid>/io` và cgroup io.stat (thường cần `sudo`)
- Optional: `smartctl` nếu dùng `--smart`

## Quick Start

```bash
chmod +x container_io_top.py watch_container_io.sh
sudo ./watch_container_io.sh --mode full --interval 5 --top 15
```

## Modes

### 1) Container throughput (iotop-like)

```bash
sudo ./container_io_top.py --mode container --interval 3 --top 20
```

### 2) Container cgroup IO (bytes + IOPS)

```bash
sudo ./container_io_top.py --mode cgroup --interval 3 --top 20
```

### 3) Device view (iostat-like)

```bash
sudo ./container_io_top.py --mode device --interval 3 --top 10
```

### 4) Full view

```bash
sudo ./container_io_top.py --mode full --interval 3 --top 10
```

### 5) Health view (root cause signals)

```bash
sudo ./container_io_top.py --mode health --interval 5 --top 10
```

## Watcher

```bash
sudo ./watch_container_io.sh --mode health --interval 5 --top 15
```

## CLI Options (`container_io_top.py`)

- `--interval <seconds>`
- `--top <N>`
- `--all`
- `--no-resolve-name`
- `--mode container|cgroup|device|full|health`
- `--include-loop`
- `--device-regex <regex>`
- `--smart` (health mode)

## CLI Options (`watch_container_io.sh`)

- `-i, --interval`
- `-t, --top`
- `-m, --mode container|cgroup|device|full|health`
- `-a, --all`
- `--no-resolve-name`
- `--include-loop`
- `--device-regex`
- `--smart`

## Environment Variables

```bash
sudo MODE=health INTERVAL=5 TOP=15 INCLUDE_ZERO=0 RESOLVE_NAME=1 SMART=0 ./watch_container_io.sh
```

- `MODE`, `INTERVAL`, `TOP`, `INCLUDE_ZERO`, `RESOLVE_NAME`
- `INCLUDE_LOOP`, `DEVICE_REGEX`, `SMART`, `PYTHON_BIN`

## Cách đọc output

### `cgroup` mode

- `RIOS/s`, `WIOS/s`: read/write IOPS
- `READ`, `WRITE`: bytes/s
- Dùng mode này để bắt case random small IO tốt hơn mode `container`

### `device` / `health` mode

- `RPS/WPS`: read/write IOPS
- `READ/WRITE`: throughput
- `%UTIL`: thời gian thiết bị bận
- `AWAIT`: latency trung bình mỗi IO
- `AVGQ`: queue depth trung bình
- `REQ_KB`: size request trung bình
- `MERGE%`: tỷ lệ merge request
- `PATTERN`: `LIKELY_SEQ`, `LIKELY_RANDOM`, `MIXED`, `IDLE` (heuristic)

### `health` mode bổ sung

- IO PSI (`/proc/pressure/io`) `some/full`
- Swap activity (`pswpin/pswpout`)
- Dirty/Writeback memory
- Top filesystem usage (`df -P`) + inode usage (`df -Pi`)
- Network mount hint (NFS/Ceph)
- `Health Alerts`: rule-based cảnh báo nguyên nhân phổ biến

## Ví dụ cho các case nghẽn IO

- Random small IO: `mode cgroup` thấy IOPS cao, `mode device` thấy `REQ_KB` nhỏ + `LIKELY_RANDOM`
- fsync/log pressure: `%UTIL` cao, `await` cao, `AVGQ` cao dù throughput không quá lớn
- Queue contention: nhiều container đều tăng IO cùng lúc, `AVGQ` tăng
- Swap/thrashing: `health` báo `pswpin/pswpout` tăng
- Disk gần đầy/full inode: `health` báo `filesystem/inode usage high`
- NFS/Ceph latency: `health` có network mounts, cần kiểm tra backend metrics

## Debug nhanh

1. Kết quả chỉ hiện sau khi hết `interval`.
2. Test nhanh:

```bash
sudo ./container_io_top.py --mode health --interval 3 --top 10 --all
```

3. Trace watcher:

```bash
sudo bash -x ./watch_container_io.sh --mode health --interval 3 --top 10
```

4. Lọc device chính:

```bash
sudo ./container_io_top.py --mode device --interval 3 --device-regex '^nvme|^sd|^vd'
```

## Limitations

- `PATTERN` seq/random là heuristic, không phải ground truth 100%.
- Muốn chính xác block-level latency/offset cần eBPF/blktrace (`biosnoop`, `biolatency`, `blktrace`).
- App-level cause (DB slow query, WAL sync, queue nội bộ) vẫn cần metric từ ứng dụng.
