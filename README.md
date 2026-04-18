# AI Infra Monitor

A production-grade infrastructure monitoring dashboard with AI-powered health analysis. Collects live system metrics, runs automated analysis, and displays everything on a real-time dashboard.

> Live metrics collection → AI analysis → real-time dashboard. Deployed on a Linux VPS behind Cloudflare.

---

## Live Demo

🔗 **[monitor.ado-runner.com](https://monitor.ado-runner.com)**

---

## What It Does

Every 15 minutes, a cron job collects live system metrics from the host server. Those metrics are sent to an **AI analysis engine**, which returns a structured health assessment. The dashboard auto-refreshes and displays everything in real time.

**Metrics collected:**
- CPU usage % with trend detection (rising/falling over last 5 readings)
- Memory usage (used / total GB)
- Disk usage per mount point
- Network I/O (cumulative sent/received)
- System uptime and process count

**AI analysis output:**
- **Status:** `green` / `yellow` / `red` with thresholds (CPU >80% = yellow, >95% = red, etc.)
- **Headline:** one-sentence plain-English summary
- **Plain-English narrative** for on-call analysts
- **Anomalies detected** (specific deviations from baseline)
- **Recommended actions**

---

## Architecture

```
VPS
├── collector.py       # cron (every 1 min) — psutil metrics → data/metrics.json
├── analyzer.py        # cron (every 5 min) — metrics → Claude API → AI analysis
├── api.py             # Flask/Gunicorn — serves /api/metrics, /api/status, /health
├── templates/
│   └── index.html     # live dashboard (vanilla JS, no framework, no build step)
└── data/
    └── metrics.json   # rolling 60-snapshot history + latest AI analysis
```

```
Browser ──► Cloudflare (SSL/DDoS) ──► Nginx (reverse proxy) ──► Gunicorn:5000
                                                                       │
                                                               Claude API (Anthropic)
```

**Key design decisions:**
- Gunicorn binds to `127.0.0.1:5000` only — never exposed directly
- Nginx handles all public traffic; Cloudflare sits in front for SSL termination and DDoS protection
- Metrics stored as flat JSON — no database dependency, simple and auditable
- AI analysis decoupled from collection — analyzer failures don't break the dashboard

---

## Tech Stack

| Layer | Technology |
|---|---|
| Metrics collection | Python 3, psutil |
| AI analysis | Anthropic Claude API (`claude-sonnet-4`) |
| API server | Flask 3, Gunicorn |
| Frontend | Vanilla HTML/CSS/JS — no framework, no build step |
| Reverse proxy | Nginx |
| CDN / SSL | Cloudflare |
| Process management | systemd |
| Scheduler | cron |
| OS / Hosting | Ubuntu 24.04 VPS |

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Live dashboard UI |
| `GET /api/metrics` | Full metrics payload + AI analysis + 60-snapshot history |
| `GET /api/status` | Lightweight health check (status, headline, key metrics) |
| `GET /health` | Liveness probe |

---

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/ohdasdiego/ai-infra-monitor.git
cd ai-infra-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 3. Verify locally

```bash
# Collect a metrics snapshot
python collector.py
# → [timestamp] Collected — CPU: X% | MEM: Y% | Processes: Z

# Run AI analysis
python analyzer.py
# → Status: GREEN — All systems operating within normal parameters

# Start the API server
gunicorn api:app --bind 0.0.0.0:5000
# → Dashboard at http://localhost:5000
```

### 4. Set up cron jobs

```bash
crontab -e
```

```cron
# Collect metrics every 15 minutes
*/15 * * * * cd /home/YOUR_USER/ai-infra-monitor && venv/bin/python collector.py >> logs/collector.log 2>&1

# Run AI analysis every 15 minutes (offset by 2 min to avoid overlap)
2-59/15 * * * * cd /home/YOUR_USER/ai-infra-monitor && venv/bin/python analyzer.py >> logs/analyzer.log 2>&1
```

### 5. Deploy as a systemd service

```bash
# Edit infra-monitor.service — replace YOUR_LINUX_USER with your username
sudo cp infra-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable infra-monitor
sudo systemctl start infra-monitor
```

### 6. Nginx reverse proxy

```bash
# Edit nginx.conf — replace YOUR_DOMAIN with your domain
sudo cp nginx.conf /etc/nginx/sites-available/ai-infra-monitor
sudo ln -s /etc/nginx/sites-available/ai-infra-monitor /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Point Cloudflare DNS to your server IP with the orange cloud (proxied) enabled.

---

## Cost Analysis

Understanding API cost at scale is critical for production deployments. Here's the projected spend at different polling intervals using `claude-sonnet-4` pricing ($3.00/M input tokens, $15.00/M output tokens):

| Interval | Calls/day | Calls/month | Input tokens/mo | Output tokens/mo | Est. cost/mo |
|---|---|---|---|---|---|
| Every 1 min | 1,440 | 43,200 | ~14.2M | ~10.8M | ~$204 |
| Every 5 min | 288 | 8,640 | ~2.8M | ~2.2M | ~$41 |
| **Every 15 min** | **96** | **2,880** | **~948K** | **~720K** | **~$14** |
| Every 30 min | 48 | 1,440 | ~474K | ~360K | ~$7 |
| Every 60 min | 24 | 720 | ~237K | ~180K | ~$4 |

> **Current config:** Every 15 minutes (~$14/month). Adjust polling frequency in `crontab` to tune cost vs. freshness.

**Per-call breakdown (15-min interval):**
- ~329 input tokens (system prompt + metrics payload)
- ~250 output tokens (structured JSON analysis)
- ~$0.005 per analysis call

**Scaling note:** At enterprise scale with hundreds of hosts, the right architecture would batch metrics from multiple servers into a single API call rather than one call per host — dramatically reducing per-host cost.

---

## Skills Demonstrated

This project is intentionally production-aligned — not a local toy:

- **Linux systems ops** — systemd service management, cron scheduling, process supervision, log routing
- **Infrastructure monitoring** — real metric collection from a live host, rolling history, threshold-based alerting logic
- **AI/API integration** — structured prompting, JSON schema enforcement, error handling, API key hygiene
- **Network operations** — Nginx reverse proxy config, Cloudflare integration, UFW firewall hardening, port exposure management
- **Incident analysis mindset** — status levels, anomaly detection, plain-English summaries for on-call engineers
- **Security hygiene** — secrets in `.env` (gitignored), gunicorn bound to loopback only, Cloudflare as the public face

---

## Author

**Diego Perez** · [github.com/ohdasdiego](https://github.com/ohdasdiego/ai-infra-monitor)
