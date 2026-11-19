# Mind at Ease Solutions — Books

Double-entry bookkeeping, job costing, payroll, invoicing and tax tools for
contractors and service businesses. One codebase serves 33 trade editions
(HVAC, plumbing, electrical, barbershop, …) and 46 market faces: each
workspace picks a niche pack (`backend/niches/*.yaml`) that drives its
branding, terminology, chart of accounts and setup checklist.

## Layout

- `backend/` — FastAPI API (MongoDB). Tests: `cd backend && python -m pytest tests/`.
- `frontend/` — React SPA (Create React App + craco).
- `docs/` — deployment and operations notes (start with `docs/DEPLOY.md`).

## Run and test

- Backend: `cd backend && pip install -r requirements.txt`, copy `backend/.env.example` to `backend/.env` (names only; set `MONGO_URL`, `DB_NAME`, `JWT_SECRET` of at least 32 characters, `FRONTEND_URL`), then `uvicorn server:app --reload`.
- Backend tests: `cd backend && python -m pytest tests/` (fixed at 2 xdist workers by `pytest.ini`). Most suites use fakes and need no services. Suites that call a live backend need `REACT_APP_BACKEND_URL`, and the tax-engine tests need `cd tax-engine && npm ci && npm run build`.
- Frontend: `cd frontend && npm install`, set `REACT_APP_BACKEND_URL`, then `npm start`, `npm test`, `npm run build`.

## Setup guides

- `STRIPE_SETUP.md` — billing keys and webhooks
- `PLAID_SETUP.md` — bank connections
- `TAXBANDITS_SETUP.md` — 1099 e-filing
- `auth_testing.md` — roles/permissions test matrix

Live: API at `bookkeeping-api-ymh6.onrender.com`, web at
`bookkeeping-web.onrender.com` (custom domain: MindAtEaseSolutions.com).
