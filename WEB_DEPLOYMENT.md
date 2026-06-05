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

Keep fixed-IP service credentials on the Raspberry Pi, not in Vercel. The Pi
queries SmartStore, Coupang, Fassto, and card services, stores the results in
SQLite, and the Vercel web app reads through `SMARTINVENTORY_MONITOR_URL`.

Optional configuration:

- `SMARTINVENTORY_MONITOR_URL_GIST`: raw gist URL used to refresh a tunnel URL,
  if your deployment flow updates the monitor URL through a gist.

## Important Vercel Constraint

Vercel serverless functions do not provide durable local SQLite storage,
fixed outbound IP, or reliable long-running background workers. The web app
therefore requires `SMARTINVENTORY_MONITOR_URL` for production use. Any local DB
fallback used inside Vercel writes to `/tmp` and is ephemeral, and fixed-IP API
calls are intentionally blocked unless they go through the Pi backend.

## Entrypoints

- Vercel: `api/index.py`
- Local web: `python -m inventory_web --open`
- Legacy desktop fallback: `python main.py`
