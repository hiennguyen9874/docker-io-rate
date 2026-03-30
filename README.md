# Docker Container IO Rate Monitor

Scripts để theo dõi container nào đang đọc/ghi đĩa nhiều, tương tự `iotop -oPa` nhưng gom theo container.

## Files

- `container_io_top.py`: thu thập I/O từ `/proc/<pid>/io`, map PID -> container, tính read/write rate theo chu kỳ lấy mẫu.
- `watch_container_io.sh`: chạy realtime vòng lặp, hiển thị bảng container name + read/write rate.

## Requirements

- Linux host có `/proc`
- Python 3
- Docker CLI (nếu muốn resolve container name)
- Quyền đọc `/proc/<pid>/io` (thường cần `sudo`)

## Quick Start

```bash
chmod +x container_io_top.py watch_container_io.sh
sudo ./watch_container_io.sh
```

Mặc định:

- interval: `30` giây
- top: `20` containers
- chỉ hiện containers có activity (`read/write > 0`)

## Usage

### 1) Chạy one-shot bằng Python

```bash
sudo ./container_io_top.py --interval 30 --top 20
```

Options:

- `--interval <seconds>`: chu kỳ lấy mẫu
- `--top <N>`: số container hiển thị
- `--all`: bao gồm cả container 0 B/s
- `--no-resolve-name`: không gọi `docker ps`, hiển thị container ID

### 2) Chạy realtime bằng Bash

```bash
sudo ./watch_container_io.sh
```

Options:

- `-i, --interval <seconds>`
- `-t, --top <N>`
- `-a, --all`
- `--no-resolve-name`

Ví dụ:

```bash
sudo ./watch_container_io.sh --interval 10 --top 10
sudo ./watch_container_io.sh --interval 5 --top 15 --no-resolve-name
```

## Configure via Environment Variables

```bash
sudo INTERVAL=15 TOP=10 INCLUDE_ZERO=1 RESOLVE_NAME=0 ./watch_container_io.sh
```

Supported vars:

- `INTERVAL` (default `30`)
- `TOP` (default `20`)
- `INCLUDE_ZERO` (`0`/`1`)
- `RESOLVE_NAME` (`0`/`1`)
- `PYTHON_BIN` (default `python3`)

## Notes

- Script đọc `read_bytes`/`write_bytes` từ `/proc/<pid>/io` và cộng theo container.
- Nếu Docker CLI không có hoặc không truy cập được daemon, script vẫn chạy nhưng hiển thị container ID.
- Có thể miss process thoáng qua giữa 2 lần snapshot.
