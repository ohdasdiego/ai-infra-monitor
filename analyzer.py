"""
analyzer.py
Reads latest metrics, sends to Claude API, writes AI analysis back to metrics.json.
Run via cron every 5 minutes (less frequent than collector to manage API usage).
"""

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ONCALL_WEBHOOK_URL = os.getenv("ONCALL_WEBHOOK_URL", "")
ONCALL_WEBHOOK_SECRET = os.getenv("ONCALL_WEBHOOK_SECRET", "")
ALERT_COOLDOWN_FILE = Path(__file__).parent / "data" / "alert_state.json"
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))

DATA_FILE = Path(__file__).parent / "data" / "metrics.json"
SKIP_CACHE_FILE = Path(__file__).parent / "data" / "skip_cache.json"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5-20251001"

# Cost-saving: skip Claude when metrics are stable and last status was green.
# Thresholds — if all metrics stay within these bands, reuse the cached analysis.
SKIP_CPU_DELTA    = float(os.getenv("SKIP_CPU_DELTA",    "10"))   # % change
SKIP_MEM_DELTA    = float(os.getenv("SKIP_MEM_DELTA",    "8"))    # % change
SKIP_DISK_DELTA   = float(os.getenv("SKIP_DISK_DELTA",   "5"))    # % change
# Always call Claude at least once every N runs regardless (keeps dashboard fresh)
SKIP_MAX_STREAK   = int(os.getenv("SKIP_MAX_STREAK",     "8"))    # ~2 hours at 15-min cron
# Hard limits — always call Claude if any metric exceeds these
SKIP_CPU_CEIL     = float(os.getenv("SKIP_CPU_CEIL",     "75"))   # %
SKIP_MEM_CEIL     = float(os.getenv("SKIP_MEM_CEIL",     "80"))   # %
SKIP_DISK_CEIL    = float(os.getenv("SKIP_DISK_CEIL",    "75"))   # %

SYSTEM_PROMPT = """You are an AI infrastructure monitoring assistant for a Network Operations Center (NOC).
You receive real-time system metrics from a Linux server and return a structured health analysis.

You must respond with ONLY valid JSON — no markdown, no explanation, no code blocks.

Your response schema:
{
  "status": "green" | "yellow" | "red",
  "headline": "one short sentence (max 12 words) summarizing overall health",
  "summary": "2-3 sentences of plain-English analysis for a NOC analyst",
  "anomalies": ["list of specific anomalies detected, empty array if none"],
  "recommendations": ["list of actionable recommendations, empty array if none"],
  "analyzed_at": "ISO 8601 timestamp"
}

Status rules:
- green: all metrics within normal range, no action needed
- yellow: one or more metrics approaching thresholds, monitor closely
- red: critical threshold breached, immediate attention required

Thresholds:
- CPU > 80% = yellow, > 95% = red
- Memory > 75% = yellow, > 90% = red
- Any disk > 80% = yellow, > 90% = red
- Process count > 300 = yellow, > 500 = red"""


def load_metrics() -> dict | None:
    if not DATA_FILE.exists():
        print("No metrics file found. Run collector.py first.")
        return None
    with open(DATA_FILE) as f:
        return json.load(f)


def build_prompt(metrics: dict) -> str:
    latest = metrics["latest"]
    history = metrics.get("history", [])

    # Compute simple CPU trend from last 5 snapshots
    recent_cpu = [h["cpu_percent"] for h in history[-5:]]
    cpu_trend = ""
    if len(recent_cpu) >= 2:
        delta = recent_cpu[-1] - recent_cpu[0]
        if delta > 20:
            cpu_trend = f" (trending UP +{delta:.1f}% over last {len(recent_cpu)} readings)"
        elif delta < -20:
            cpu_trend = f" (trending DOWN {delta:.1f}% over last {len(recent_cpu)} readings)"

    disk_lines = "\n".join(
        f"  - {d['mountpoint']}: {d['percent']}% used ({d['used_gb']}GB / {d['total_gb']}GB)"
        for d in latest["disks"]
    )

    return f"""Current server metrics snapshot:

Timestamp: {latest['timestamp']}
CPU Usage: {latest['cpu_percent']}%{cpu_trend}
Memory: {latest['memory']['percent']}% used ({latest['memory']['used_gb']}GB / {latest['memory']['total_gb']}GB)
Disk Usage:
{disk_lines}
Network I/O: Sent {latest['network']['bytes_sent_mb']}MB / Received {latest['network']['bytes_recv_mb']}MB (cumulative since boot)
System Uptime: {latest['uptime_hours']} hours
Running Processes: {latest['process_count']}

Analyze these metrics and return your structured JSON response."""


def analyze(metrics: dict) -> dict:
    prompt = build_prompt(metrics)

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 400,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    response.raise_for_status()

    raw = response.json()["content"][0]["text"].strip()

    # Strip markdown fences if model includes them
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    analysis = json.loads(raw)
    analysis["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    return analysis


def save_analysis(analysis: dict, metrics: dict):
    metrics["ai_analysis"] = analysis
    with open(DATA_FILE, "w") as f:
        json.dump(metrics, f, indent=2)


def load_skip_cache() -> dict:
    """Load the skip-cache state (last analyzed metrics + streak counter)."""
    if SKIP_CACHE_FILE.exists():
        try:
            with open(SKIP_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_skip_cache(state: dict):
    SKIP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SKIP_CACHE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_skip_claude(metrics: dict, cache: dict) -> tuple[bool, str]:
    """
    Returns (skip, reason).
    Skip Claude if:
      - Last status was green
      - All key metrics are within stable delta bands
      - No metric exceeds the hard ceiling
      - Skip streak hasn't exceeded SKIP_MAX_STREAK
    """
    if not cache:
        return False, "no cache"

    last_status = cache.get("last_status", "")
    if last_status != "green":
        return False, f"last status was {last_status}"

    streak = cache.get("skip_streak", 0)
    if streak >= SKIP_MAX_STREAK:
        return False, f"max streak reached ({streak})"

    latest = metrics.get("latest", {})
    prev   = cache.get("last_metrics", {})

    if not prev:
        return False, "no previous metrics"

    cpu  = latest.get("cpu_percent", 0)
    mem  = latest.get("memory", {}).get("percent", 0)
    disk = max((d.get("percent", 0) for d in latest.get("disks", [])), default=0)

    # Hard ceiling check — don't skip if approaching thresholds
    if cpu  >= SKIP_CPU_CEIL:  return False, f"CPU at {cpu}% (ceil {SKIP_CPU_CEIL}%)"
    if mem  >= SKIP_MEM_CEIL:  return False, f"Memory at {mem}% (ceil {SKIP_MEM_CEIL}%)"
    if disk >= SKIP_DISK_CEIL: return False, f"Disk at {disk}% (ceil {SKIP_DISK_CEIL}%)"

    # Delta check — skip only if metrics haven't moved much
    prev_cpu  = prev.get("cpu", cpu)
    prev_mem  = prev.get("mem", mem)
    prev_disk = prev.get("disk", disk)

    if abs(cpu  - prev_cpu)  > SKIP_CPU_DELTA:  return False, f"CPU delta {abs(cpu-prev_cpu):.1f}%"
    if abs(mem  - prev_mem)  > SKIP_MEM_DELTA:  return False, f"Mem delta {abs(mem-prev_mem):.1f}%"
    if abs(disk - prev_disk) > SKIP_DISK_DELTA: return False, f"Disk delta {abs(disk-prev_disk):.1f}%"

    return True, f"stable green (streak {streak+1}/{SKIP_MAX_STREAK})"


def update_skip_cache(cache: dict, metrics: dict, status: str, skipped: bool) -> dict:
    latest = metrics.get("latest", {})
    cpu  = latest.get("cpu_percent", 0)
    mem  = latest.get("memory", {}).get("percent", 0)
    disk = max((d.get("percent", 0) for d in latest.get("disks", [])), default=0)

    cache["last_status"] = status
    cache["last_metrics"] = {"cpu": cpu, "mem": mem, "disk": disk}
    cache["skip_streak"] = (cache.get("skip_streak", 0) + 1) if skipped else 0
    cache["last_updated"] = datetime.now(timezone.utc).isoformat()
    return cache


def load_alert_state() -> dict:
    """Load cooldown state to avoid duplicate alerts."""
    if ALERT_COOLDOWN_FILE.exists():
        try:
            with open(ALERT_COOLDOWN_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_alert_state(state: dict):
    with open(ALERT_COOLDOWN_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_alert(status: str, state: dict) -> bool:
    """Return True if enough time has passed since last alert."""
    if status not in ("red",):
        return False
    last_alert = state.get("last_alert_at")
    if not last_alert:
        return True
    try:
        last_dt = datetime.fromisoformat(last_alert)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return elapsed >= ALERT_COOLDOWN_MINUTES
    except Exception:
        return True


def fire_webhook(analysis: dict, metrics: dict):
    """POST alert to On-Call Assistant webhook."""
    if not ONCALL_WEBHOOK_URL:
        return

    status = analysis.get("status", "green")
    severity_map = {"red": "high", "yellow": "medium"}
    severity = severity_map.get(status, "low")

    latest = metrics.get("latest", {})
    cpu = latest.get("cpu_percent", "?")
    mem = latest.get("memory", {}).get("percent", "?")

    payload = {
        "title": f"Infra Monitor: {analysis.get('headline', 'Anomaly detected')}",
        "description": (
            f"{analysis.get('summary', '')}\n\n"
            f"CPU: {cpu}% | Memory: {mem}%\n"
            f"Anomalies: {', '.join(analysis.get('anomalies', []) or ['none'])}"
        ),
        "severity": severity,
        "host": "claw-gateway1",
        "metric": "multi",
        "value": cpu
    }

    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}

    if ONCALL_WEBHOOK_SECRET:
        sig = hmac.new(ONCALL_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    try:
        resp = requests.post(ONCALL_WEBHOOK_URL, data=body, headers=headers, timeout=10)
        resp.raise_for_status()
        print(f"Webhook fired → On-Call Assistant (incident #{resp.json().get('incident_id', '?')} created)")
    except Exception as e:
        print(f"Webhook failed: {e}")


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        exit(1)

    metrics = load_metrics()
    if not metrics:
        exit(1)

    skip_cache = load_skip_cache()
    skip, reason = should_skip_claude(metrics, skip_cache)

    if skip:
        # Reuse last cached analysis — update timestamp so dashboard shows fresh
        analysis = metrics.get("ai_analysis", {})
        if analysis:
            analysis["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            analysis["_cache_hit"] = True
            save_analysis(analysis, metrics)
            print(f"[SKIP] Reusing cached analysis — {reason}")
            print(f"Status: {analysis.get('status','?').upper()} — {analysis.get('headline','cached')}")
            skip_cache = update_skip_cache(skip_cache, metrics, analysis.get("status", "green"), skipped=True)
            save_skip_cache(skip_cache)
            exit(0)
        # No cached analysis yet — fall through to Claude
        print("[SKIP] No cache available yet — calling Claude")

    print("Sending metrics to Claude for analysis...")
    try:
        analysis = analyze(metrics)
        analysis.pop("_cache_hit", None)
        save_analysis(analysis, metrics)
        skip_cache = update_skip_cache(skip_cache, metrics, analysis.get("status", "green"), skipped=False)
        save_skip_cache(skip_cache)
        print(f"Status: {analysis['status'].upper()} — {analysis['headline']}")
        if analysis["anomalies"]:
            print(f"Anomalies: {', '.join(analysis['anomalies'])}")
        if analysis["recommendations"]:
            print(f"Recommendations: {', '.join(analysis['recommendations'])}")

        # Fire webhook to On-Call Assistant if status is red only (yellow skipped)
        state = load_alert_state()
        if should_alert(analysis["status"], state):
            fire_webhook(analysis, metrics)
            state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
            state["last_status"] = analysis["status"]
            save_alert_state(state)
            print(f"Alert state updated. Next alert in {ALERT_COOLDOWN_MINUTES} min minimum.")
        elif analysis["status"] in ("red",):
            print(f"Alert suppressed — cooldown active (last alert: {state.get('last_alert_at', 'unknown')})")
        else:
            # Green/yellow status — reset cooldown so next red fires immediately
            if state.get("last_status") in ("red",):
                state["last_alert_at"] = None
                state["last_status"] = "green"
                save_alert_state(state)
                print("Status back to green — cooldown reset.")

    except requests.exceptions.HTTPError as e:
        print(f"Claude API error: {e.response.status_code} — {e.response.text[:200]}")
        exit(1)
    except Exception as e:
        print(f"Analysis failed: {e}")
        exit(1)
