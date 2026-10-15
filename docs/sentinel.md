# Sentinel — the 24/7 security watcher

Sentinel is a scheduled job that watches the app for signs of intrusion or
misuse and raises in-app security alerts (Settings → Security alerts), plus
email digests to workspace owners when SMTP is configured.

## What it watches

| Check | What trips it |
|---|---|
| Brute-force spike | ≥10 failed logins from one IP in 15 min |
| Off-hours admin | Sensitive actions (fiscal close, payout changes, session revocation, backup import, inventory toggle) between midnight–5am local time |
| PII burst | ≥10 payout-account/TIN views per workspace in one hour |
| Stale backup | No backup export in 7+ days (only for workspaces that used to export) |
| Digest | Unacknowledged critical alerts emailed to owners |

Findings are de-duplicated (one alert per type per workspace per hour).

## Honest scope

Sentinel is **application-layer** monitoring — it sees what happens *inside*
the app: logins, API abuse patterns, data-access anomalies. It does not see
network scans, OS-level intrusions, or DDoS. Those belong to the hosting
provider (most PaaS/cloud hosts include network IDS and DDoS protection —
verify yours does before promising customers "24/7 monitoring").

## Running it

**Option A — cron** (simplest):

```cron
*/15 * * * *  cd /opt/bookkeeping/backend && /usr/bin/python3 sentinel.py >> /var/log/sentinel.log 2>&1
```

**Option B — systemd timer** (better logging/retries):

`sentinel.service`:
```ini
[Unit]
Description=Bookkeeping security sentinel
[Service]
Type=oneshot
WorkingDirectory=/opt/bookkeeping/backend
ExecStart=/usr/bin/python3 sentinel.py
EnvironmentFile=/opt/bookkeeping/backend/.env
```

`sentinel.timer`:
```ini
[Unit]
Description=Run sentinel every 15 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
```

Then: `systemctl enable --now sentinel.timer`

**Manual run** (owner): `POST /api/alerts/sentinel/run`, or
`python3 backend/sentinel.py` from the repo.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `SENTINEL_TZ` | `America/Chicago` | Local timezone for the off-hours window |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | unset (alerts in-app only) | Email digests |
| `MONGO_URL` etc. | — | Same DB config the API server uses |

The script needs the same `.env` as the API server so it can reach MongoDB.
