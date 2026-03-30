# Docker Container + Disk IO Monitor

Scripts để theo dõi container nào đang đọc/ghi đĩa nhiều, đồng thời theo dõi nghẽn IO ở level device và ước lượng pattern `sequential/random`.

## Files

- `container_io_top.py`
  - `container` mode: tương tự `iotop -oPa` nhưng gom theo container.
  - `device` mode: chỉ số kiểu `iostat -x` từ `/proc/diskstats`.
  - `full` mode: in cả container + device trong cùng 1 chu kỳ.
- `watch_container_io.sh`
  - realtime loop, refresh mỗi `interval` giây.

## Requirements

- Linux host có `/proc` và `/sys/block`
- Python 3
- Docker CLI (nếu muốn resolve container name)
- Quyền đọc `/proc/<pid>/io` (thường cần `sudo`)

## Quick Start

```bash
chmod +x container_io_top.py watch_container_io.sh
sudo ./watch_container_io.sh --mode full --interval 5 --top 15
```

## Usage

### 1) One-shot

```bash
# Container throughput
sudo ./container_io_top.py --mode container --interval 3 --top 20

# Device metrics + pattern heuristic
sudo ./container_io_top.py --mode device --interval 3 --top 10

# Cả 2
sudo ./container_io_top.py --mode full --interval 3 --top 10
```

### 2) Realtime watcher

```bash
sudo ./watch_container_io.sh --mode full --interval 5 --top 15
```

## Options (`container_io_top.py`)

- `--interval <seconds>`: chu kỳ lấy mẫu
- `--top <N>`: số dòng hiển thị
- `--all`: bao gồm cả row 0 activity
- `--no-resolve-name`: không gọi `docker ps`
- `--mode container|device|full`
- `--include-loop`: include `loop*`/`ram*` khi mode device/full
- `--device-regex <regex>`: lọc device name (vd `'^nvme|^sd'`)

## Options (`watch_container_io.sh`)

- `-i, --interval`
- `-t, --top`
- `-m, --mode container|device|full`
- `-a, --all`
- `--no-resolve-name`
- `--include-loop`
- `--device-regex`

## Environment Variables

```bash
sudo MODE=full INTERVAL=5 TOP=15 INCLUDE_ZERO=0 RESOLVE_NAME=1 ./watch_container_io.sh
```

- `MODE` (`container|device|full`)
- `INTERVAL` (default `30`)
- `TOP` (default `20`)
- `INCLUDE_ZERO` (`0`/`1`)
- `RESOLVE_NAME` (`0`/`1`)
- `INCLUDE_LOOP` (`0`/`1`)
- `DEVICE_REGEX` (default empty)
- `PYTHON_BIN` (default `python3`)

## Cách đọc output device mode

- `READ/WRITE`: throughput theo device
- `RPS/WPS`: số read/write IO mỗi giây (IOPS)
- `%UTIL`: phần trăm thời gian device bận xử lý IO
- `AWAIT` (ms): latency trung bình mỗi IO
- `AVGQ`: queue depth trung bình
- `REQ_KB`: kích thước request trung bình
- `MERGE%`: tỷ lệ request được merge ở block layer
- `PATTERN`: `LIKELY_SEQ` / `LIKELY_RANDOM` / `MIXED` / `IDLE` (heuristic)

## Quan trọng về sequential vs random

`PATTERN` chỉ là ước lượng từ `REQ_KB` + `MERGE%`, không phải ground truth 100%.
Nếu cần chính xác cao, dùng thêm eBPF/blktrace (`biosnoop`, `biolatency`, `blktrace`) để thấy block offset theo thời gian.

## Debug nhanh khi "không hiện gì"

1. Script chỉ in kết quả sau khi hết `interval`.
2. Dùng interval ngắn để test:

```bash
sudo ./container_io_top.py --mode full --interval 3 --top 10 --all
```

3. Bật trace shell watcher:

```bash
sudo bash -x ./watch_container_io.sh --mode full --interval 3 --top 10
```

4. Lọc device chính:

```bash
sudo ./container_io_top.py --mode device --interval 3 --device-regex '^nvme|^sd|^vd'
```

## Limitations

- Container mode dựa trên cộng `/proc/<pid>/io`, có thể miss process rất ngắn sống giữa 2 snapshot.
- Không map trực tiếp được random/seq theo từng container chỉ bằng `/proc/<pid>/io`.
- Với overlay/network storage, latency có thể đến từ tầng storage backend, không chỉ local disk.
