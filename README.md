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

## How It Works

1. Script chụp snapshot tổng `read_bytes/write_bytes` của từng container tại thời điểm `T0`.
2. Chờ đúng `interval` giây.
3. Chụp snapshot lần 2 tại `T1`.
4. Tính throughput: `(bytes_T1 - bytes_T0) / interval`.
5. Sắp xếp theo tổng IO (`read + write`) và hiển thị top N.

Lưu ý quan trọng: kết quả chỉ xuất hiện **sau khi hết interval**.

## Quick Start

```bash
chmod +x container_io_top.py watch_container_io.sh
sudo ./watch_container_io.sh --interval 3 --top 20
```

Mặc định watcher:

- interval: `30` giây
- top: `20` containers
- chỉ hiện containers có activity (`read/write > 0`)

## Usage

### 1) One-shot bằng Python

```bash
sudo ./container_io_top.py --interval 30 --top 20
```

Options:

- `--interval <seconds>`: chu kỳ lấy mẫu
- `--top <N>`: số container hiển thị
- `--all`: bao gồm cả container 0 B/s
- `--no-resolve-name`: không gọi `docker ps`, hiển thị container ID

### 2) Realtime loop bằng Bash

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
sudo ./watch_container_io.sh --interval 3 --top 20 --all
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

## Practical Debug Guide

### Symptom: "Không thấy gì", rồi Ctrl+C ra `KeyboardInterrupt`

Nguyên nhân: bạn dừng trước khi hết `interval`.

Ví dụ nếu `interval=30`, script cần chờ ~30 giây mới in bảng.

Cách xử lý:

```bash
sudo ./watch_container_io.sh --interval 3 --top 10
```

Chờ tối thiểu 1 chu kỳ (3 giây trong ví dụ) trước khi kết luận không có output.

### Debug Checklist

1. Kiểm tra script one-shot có dữ liệu:

```bash
sudo ./container_io_top.py --interval 3 --top 20 --all --no-resolve-name
```

2. Kiểm tra Docker containers đang chạy:

```bash
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Status}}'
```

3. Kiểm tra cgroup path có docker scope:

```bash
sudo sh -c "grep -H 'docker-.*scope' /proc/[0-9]*/cgroup | head -n 30"
```

4. Nếu muốn trace watcher:

```bash
sudo bash -x ./watch_container_io.sh --interval 3 --top 10
```

### Tạo IO test để xác minh

Chạy lệnh ghi dữ liệu trong một container:

```bash
docker exec -it <container_name> sh -c 'dd if=/dev/zero of=/tmp/io-test bs=1M count=200 oflag=direct; sync'
```

Sau đó chạy lại monitor để thấy write rate tăng.

## Notes / Limitations

- Script đọc `read_bytes`/`write_bytes` từ `/proc/<pid>/io` và cộng theo container.
- Nếu Docker CLI không có hoặc không truy cập daemon, script vẫn chạy nhưng hiển thị container ID.
- Có thể miss process rất ngắn sống giữa 2 snapshot.
- Nếu host dùng runtime/cgroup layout khác biệt lớn, parser có thể cần điều chỉnh regex/path.
