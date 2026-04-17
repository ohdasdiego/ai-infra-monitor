"""
analyzer.py
Reads latest metrics, sends to Claude API, writes AI analysis back to metrics.json.
Run via cron every 5 minutes (less frequent than collector to manage API usage).
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_FILE = Path(__file__).parent / "data" / "metrics.json"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-20250514"

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
        if delta > 10:
            cpu_trend = f" (trending UP +{delta:.1f}% over last {len(recent_cpu)} readings)"
        elif delta < -10:
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
            "max_tokens": 1000,
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


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        exit(1)

    metrics = load_metrics()
    if not metrics:
        exit(1)

    print("Sending metrics to Claude for analysis...")
    try:
        analysis = analyze(metrics)
        save_analysis(analysis, metrics)
        print(f"Status: {analysis['status'].upper()} — {analysis['headline']}")
        if analysis["anomalies"]:
            print(f"Anomalies: {', '.join(analysis['anomalies'])}")
        if analysis["recommendations"]:
            print(f"Recommendations: {', '.join(analysis['recommendations'])}")
    except Exception as e:
        print(f"Analysis failed: {e}")
        exit(1)
