# SmartInventory Web Deployment

SmartInventory now has a Vercel-ready web app in `inventory_web`.

## Local Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m inventory_web --open
```

Default local URL:

```text
http://127.0.0.1:8766
```

## Vercel

`vercel.json` routes all requests to `api/index.py`, which exposes the FastAPI
app. There is no Node or React build step.

Required Vercel environment variable:

- `SMARTINVENTORY_MONITOR_URL`: public HTTPS URL for the existing monitor/Pi
  backend. Vercel should use this as the persistent data source and fixed-IP
  API gateway.
- `DISCORD_INVENTORY_WEBHOOK_URL`: Discord webhook URL that receives the daily
  inventory report.
- `CRON_SECRET`: random secret used by Vercel Cron. Vercel sends it as
  `Authorization: Bearer <CRON_SECRET>` and the report endpoint verifies it.

Keep fixed-IP service credentials on the Raspberry Pi, not in Vercel. The Pi
queries SmartStore, Coupang, Fassto, and card services, stores the results in
SQLite, and the Vercel web app reads through `SMARTINVENTORY_MONITOR_URL`.

Optional configuration:

- `SMARTINVENTORY_MONITOR_URL_GIST`: raw gist URL used to refresh a tunnel URL,
  if your deployment flow updates the monitor URL through a gist.
- `DISCORD_WEBHOOK_URL`: fallback Discord webhook URL if
  `DISCORD_INVENTORY_WEBHOOK_URL` is not set.

## Daily Discord Inventory Report

`vercel.json` registers this Cron Job:

```json
{
  "path": "/api/reports/inventory/discord",
  "schedule": "0 15 * * *"
}
```

Vercel schedules cron expressions in UTC, so `0 15 * * *` runs at 00:00 KST.
The endpoint sends only 상품관리 master items. Unlinked Naver/Coupang channel
items are deliberately excluded.

Manual dry run:

```text
https://<your-domain>/api/reports/inventory/discord?dry_run=1
```

## Important Vercel Constraint

Vercel serverless functions do not provide durable local SQLite storage,
fixed outbound IP, or reliable long-running background workers. The web app
therefore requires `SMARTINVENTORY_MONITOR_URL` for production use. Any local DB
fallback used inside Vercel writes to `/tmp` and is ephemeral, and fixed-IP API
calls are intentionally blocked unless they go through the Pi backend.

## Entrypoints

- Vercel: `api/index.py`
- Local web: `python -m inventory_web --open`

## Repository Diet

Desktop build artifacts, PySide UI files, app icons, Android helper sources,
debug dumps, local credentials, and SQLite DB files are excluded from deployment.
The Vercel bundle should only need `api/`, `inventory_web/`, the web-used
`inventory_app/` service code, `requirements.txt`, and `vercel.json`.
