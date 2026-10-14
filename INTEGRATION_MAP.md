# Bookkeeping integration map (work branch)

Source repositories:
- Target: `ccempireproject100K/Bookkeeping-start-add-on` (React 19 + Create React App/CRACO, JavaScript, FastAPI/MongoDB)
- Reference: `ccempireproject100K/Barber-addition-bookkeeping-` (React 19 + Vite/TypeScript, FastAPI/MongoDB)

## Already wired in target (route declarations, not verified running)
`frontend/src/App.js` declares routes for dashboard, jobs, customers, estimates, chart of accounts, journal, general ledger, banking, trial balance, balance sheet, profit and loss, cash flow, budgets, reports, fiscal periods, inventory, quick sell, invoices, bills, expenses, aging, gift cards, commissions, and cash close.
`backend/server.py` includes auth, ledger, products, POS, invoices, cash close, dashboard, bills, aging, gift cards, commissions, customers, jobs, banking, vendors, reports, and estimates routers.

## Potential source-only features to inspect before importing
The reference `frontend/src/App.tsx` declares routes for team, settings, procurement, audit log, password reset, login/signup, payments, clients, inventory labels, inventory movements, stock count and product details.
The reference `backend/server.py` mounts AI, billing, recurring, payments, cash, inventory, and bookkeeping routers.

## Wiring requirements
1. Check existing target pages, API client, auth context, backend router endpoints and models before copying any files.
2. Inventory all source-only pages plus dependencies; do not mix Vite/TypeScript files unmodified into a CRA/JavaScript app.
3. For each useful source-only feature, port its page and imports to the target frontend, add a protected route and nav item, and adapt the backend router to the target's database/auth/tenant interfaces.
4. Preserve tenant separation; do not replace the target auth flows or DB schema wholesale.
5. Test user authentication, route loading, CRUD, API contracts, database indexes, and cross-tenant security.
6. Run production build and smoke tests before merging into `main`.

## Status
Mapping started; no functionality has been merged or confirmed working on this branch. Original projects remain unchanged. 
