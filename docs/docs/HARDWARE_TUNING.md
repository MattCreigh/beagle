# Hardware Tuning Guide

This guide covers host tuning for local deployments. All settings apply to a
generic Linux host running the orchestrator; values that depend on the host
hardware (block device names, mount paths) are written as placeholders.

**Host profile (placeholder):**

- **CPU:** multi-core x86_64 CPU
- **RAM:** 16GB DDR (or larger)
- **Primary storage:** NVMe SSD (OS + code)
- **Secondary storage:** SATA SSD / HDD (RAG data, backups)

---

## Recommended OS Settings

### 1. Transparent Huge Pages (THP)

Enables 2MB memory pages for reduced TLB miss rate:

```bash
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/defrag
```

Verify:

```bash
cat /sys/kernel/mm/transparent_hugepage/enabled
# Output should show: always [madvise] never → "always" in brackets
```

### 2. I/O Schedulers

**NVMe:** Use `none` (noop) — NVMe has native NCQ, scheduler adds overhead:

```bash
echo none | sudo tee /sys/block/<nvme_device>/queue/scheduler
```

**SATA SSD:** Use `mq-deadline` — optimal for SSDs with rotational media fallback:

```bash
echo mq-deadline | sudo tee /sys/block/<sata_device>/queue/scheduler
```

### 3. CPU Governor

During active workflows, use `performance` governor:

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

When idle (>5 min), switch to `powersave`:

```bash
echo powersave | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

The orchestrator's `cpu_governor.py` module handles this automatically.

### 4. Ramdisk for RAG Staging

Create a tmpfs mount for ingestion intermediates (recommended ~6GB):

```bash
sudo mkdir -p <ramdisk_mount>
sudo mount -t tmpfs -o size=6G tmpfs <ramdisk_mount>
```

Persist across reboots (`/etc/fstab`):

```text
tmpfs <ramdisk_mount> tmpfs defaults,size=6G 0 0
```

**Impact:** Reduces NVMe writes by 70-90% during RAG ingestion.
Only the final atomic rename touches persistent storage.

### 5. ZRAM Swap (Emergency)

Zstd-compressed swap device for memory pressure emergencies (e.g. 8GB):

```bash
sudo modprobe zram
echo zstd | sudo tee /sys/block/zram0/comp_algorithm
echo 8G | sudo tee /sys/block/zram0/disksize
sudo mkswap /dev/zram0
sudo swapon -p 100 /dev/zram0
```

**Note:** ZRAM is NOT configured by default. Enable only if needed.

---

## Verification

Run the tuning script:

```bash
# Check current settings
sudo bash scripts/tune_system.sh --check

# Apply all recommended settings
sudo bash scripts/tune_system.sh
```

---

## Config.toml Integration

All hardware settings live in `config.toml` under `[hardware]`:

```toml
[hardware]
ramdisk_enabled = true
ramdisk_path = "<ramdisk_mount>"
ramdisk_size_mb = 6144
dynamic_concurrency = true
concurrency_min = 2
concurrency_max = 6
cpu_high_threshold = 80
cpu_low_threshold = 30
warm_workers_enabled = true
warm_worker_count = 2
incremental_ingest = true
ssd_write_saving_log = true
zram_enabled = false
zram_size_mb = 8192
```

---

## Performance Expectations

| Optimization | Expected Impact |
|---|---|
| Ramdisk staging | 70-90% NVMe write reduction during ingestion |
| Incremental ingestion | 2-10x faster re-ingestion (unchanged files skipped) |
| Dynamic concurrency | Better CPU utilization (2-6 workers adaptive) |
| Warm workers | ~50% reduction in cold-start latency per node |
| THP + proper I/O sched | 5-15% general performance improvement |
| CPU governor management | 10-20% faster workflow execution in performance mode |
