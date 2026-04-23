"""
collector.py
Gathers system metrics and writes them to data/metrics.json.
Run via cron every 60 seconds.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

DATA_FILE = Path(__file__).parent / "data" / "metrics.json"
MAX_HISTORY = 60  # keep last 60 snapshots (~1 hour at 1/min)


def collect() -> dict:
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    net = psutil.net_io_counters()

    disks = []
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint.startswith("/snap/"):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mountpoint": part.mountpoint,
                "total_gb": round(usage.total / 1e9, 1),
                "used_gb": round(usage.used / 1e9, 1),
                "percent": usage.percent,
            })
        except PermissionError:
            continue

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu_percent": cpu,
        "memory": {
            "total_gb": round(mem.total / 1e9, 1),
            "used_gb": round(mem.used / 1e9, 1),
            "percent": mem.percent,
        },
        "disks": disks,
        "network": {
            "bytes_sent_mb": round(net.bytes_sent / 1e6, 2),
            "bytes_recv_mb": round(net.bytes_recv / 1e6, 2),
        },
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "process_count": len(psutil.pids()),
    }


def load_history() -> list:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                data = json.load(f)
                return data.get("history", [])
        except (json.JSONDecodeError, KeyError):
            return []
    return []


def save(snapshot: dict, history: list):
    history.append(snapshot)
    history = history[-MAX_HISTORY:]  # trim to max

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump({
            "latest": snapshot,
            "history": history,
            "last_updated": snapshot["timestamp"],
        }, f, indent=2)


if __name__ == "__main__":
    snapshot = collect()
    history = load_history()
    save(snapshot, history)
    print(f"[{snapshot['timestamp']}] Collected — CPU: {snapshot['cpu_percent']}% | "
          f"MEM: {snapshot['memory']['percent']}% | "
          f"Processes: {snapshot['process_count']}")
