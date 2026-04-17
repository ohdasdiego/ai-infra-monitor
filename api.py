"""
api.py
Lightweight Flask API that serves metrics data to the dashboard.
Run with: gunicorn api:app --bind 0.0.0.0:5000
"""

import json
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

DATA_FILE = Path(__file__).parent / "data" / "metrics.json"


def load_data() -> dict:
    if not DATA_FILE.exists():
        return {"error": "No metrics data yet. Run collector.py first."}
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {"error": "Metrics file is malformed."}


@app.route("/api/metrics")
def metrics():
    """Full metrics payload including history and AI analysis."""
    return jsonify(load_data())


@app.route("/api/status")
def status():
    """Lightweight status check — just current health and headline."""
    data = load_data()
    if "error" in data:
        return jsonify(data), 503

    analysis = data.get("ai_analysis", {})
    latest = data.get("latest", {})

    return jsonify({
        "status": analysis.get("status", "unknown"),
        "headline": analysis.get("headline", "No analysis yet"),
        "last_updated": data.get("last_updated"),
        "analyzed_at": analysis.get("analyzed_at"),
        "cpu_percent": latest.get("cpu_percent"),
        "memory_percent": latest.get("memory", {}).get("percent"),
    })


@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/health")
def health():
    """Simple liveness probe."""
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
