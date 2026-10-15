# Deploying the Bookkeeping App

The backend needs outbound database access (this dev sandbox blocks it),
so live verification happens on a host like Render.

## Option A: Render Blueprint (recommended)

1. Sign up at [render.com](https://render.com) (free tier works for testing).
2. Dashboard → **New** → **Blueprint** → connect the
   `ccempireproject100K/Bookkeeping-start-add-on` repo.
3. Render reads `render.yaml` and creates two services:
   `bookkeeping-api` (Python) and `bookkeeping-web` (static React).
4. When prompted for environment variables:
   - `MONGO_URL` → your Atlas connection string
     (`mongodb+srv://...`)
   - `DB_NAME` → `bookkeeping` (pre-filled)
   - `JWT_SECRET` → auto-generated
   - `FRONTEND_URL` → the `bookkeeping-web` URL Render gives you
     (add after first deploy, e.g. `https://bookkeeping-web.onrender.com`)
   - `REACT_APP_BACKEND_URL` (on the web service) → the `bookkeeping-api`
     URL Render gives you (e.g. `https://bookkeeping-api.onrender.com`)
5. Deploy. The API services `/api`, the web service serves the app.

## Option B: Docker (any host)

```bash
docker build -t books ./backend
docker run -p 8000:8000 -e PORT=8000 \
  -e MONGO_URL='mongodb+srv://...' \
  -e DB_NAME=bookkeeping \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  books
```

Build the frontend separately (`cd frontend && npm run build`) with
`REACT_APP_BACKEND_URL` pointing at the API, and host `frontend/build`
on any static host.

## After deploy: live verification checklist

- Register a workspace, log in
- Create a product, PO → receive → convert to bill
- Issue an invoice, record a payment
- Check the ledger balances and run the CPA package export
- Invite flow, copilot propose/approve, collections run

## Scheduled jobs (collections autopilot)

The backend can run the collections autopilot on a daily schedule via an
in-app scheduler (APScheduler). It is **off by default**. Environment
variables on the API service:

- `ENABLE_SCHEDULER` → `true` to start the scheduler (default off)
- `COLLECTIONS_RUN_TIME` → daily run time `HH:MM`, 24h (default `06:00`)
- `SCHEDULER_TZ` → IANA timezone for the schedule
  (default `America/Chicago`)

Notes:

- Only one app instance runs the schedule at a time (MongoDB lock in
  `scheduler_locks`, self-healing via TTL).
- Each workspace gets at most one autopilot run per day (`scheduler_runs`
  records); the autopilot itself is stage-idempotent per invoice.
- Only workspaces with the autopilot enabled in
  `/collections/config` are processed.
- Check status any time: `GET /api/collections/scheduler/status`
  (owner/manager).
