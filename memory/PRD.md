# The Barber's Ledger — PRD

## Original problem statement
Build "The Barber's Ledger", a double-entry bookkeeping + POS platform for barbershops. The user supplied a full handoff spec (CHATGPT_HANDOFF / SPEC / AUDIT_AND_PLAN / PHASE_2A_DESIGN) describing a Phase-1 single-entry app and asked to add the **Phase 2a double-entry core**. No runnable Phase-1 code was ever provided (only docs), so — per the user's explicit choice — the app was **built fresh as one self-contained application** with the double-entry ledger as the centerpiece plus the operational layer that feeds it. User choices: **hard-cutover** double-entry (ledger is the primary book), build immediately, no LLM, use the environment MongoDB, "make it a marvel of modern bookkeeping".

## Architecture
- **Backend**: FastAPI + MongoDB (motor). `lib/base.py` (ids, Decimal-safe integer-cents money, `Scoped` tenant isolation), `lib/auth.py` (JWT httpOnly Lax cookie, role permission map), `lib/coa.py` (chart-of-accounts seed), `lib/ledger.py` (posting, gapless numbering, period locking, trial balance / balance sheet / P&L / GL), `lib/posting.py` (event→journal-entry rule registry). Routers: auth, ledger, products, pos, invoices, cashclose, dashboard. All under `/api`.
- **Frontend**: React + Tailwind + shadcn/ui. Dark "obsidian + brass" theme (Outfit / DM Sans / JetBrains Mono). AuthContext + protected routes + sidebar layout with a Role Switcher.
- **Money**: stored as integer cents; balanced invariant enforced in Python before every Mongo write.

## User personas
- **Owner/Admin** — full control (post/approve/reverse, periods, accounts, POS, invoices, cash close).
- **Accountant** — read-only ledger & reports.
- **Bookkeeper** — read + create draft entries (cannot approve/post).
- **Barber/Staff** — Quick Sell POS only.

## Core requirements (static)
Double-entry ledger with balanced debit=credit invariant, chart of accounts, journal entries (draft/post/reverse, append-only), general ledger, trial balance, balance sheet, P&L, fiscal periods with soft-close/lock (423 on locked post), opening-balance migration, tenant isolation, role permissions.

## Implemented (2026-06)
### HVAC Contractor pivot — Phase 1: Advanced Job Costing (NEW, tested 16/16)
- App rebranded to **Mind at Ease Solutions HVAC**; `barber` role surfaced as "Technician".
- Jobs: unique gapless `JOB-####`, customers + service locations, types installation/maintenance/repair, estimate→active→completed→closed lifecycle.
- Cost tracking by category (material/labor/equipment/subcontractor/other); labor by hours×rate with technician; material-from-inventory relieves 1300. **Every cost/revenue posts exactly ONE journal entry** (no duplicates) into the double-entry engine.
- Change orders, job invoicing (revenue → 4300 Contract Revenue), estimate-vs-actual variance.
- Profitability engine: revenue/cost/gross profit/margin per job; reports grouped by job / customer / technician / service type; unprofitable jobs flagged. Verified spec example (JOB-0001 $8,000→$5,500→$2,500→31.25%). Books stay balanced.
- **Job Profitability Dashboard** (`GET /jobs/reports/dashboard`): KPI tiles (total GP, avg margin, profitable vs unprofitable counts) + a "Jobs Needing Attention" panel listing loss-making and low-margin (<20%) jobs + top profitable jobs. Completes Priority 1.
- New accounts: 4300, 5000/5005/5006/5007/5008 (job costs), 2320 Wages Payable, 2330 Customer Deposits.

### Accounting core & add-ons
- JWT auth (email/password) + demo logins + Role Switcher + tenant isolation
- ✅ Chart of Accounts (39 barbershop accounts seeded) + custom account create/archive
- ✅ Journal Entries: balanced-only posting, gapless `JE-{FY}-{seq}` numbering, draft→post, linked reversals
- ✅ Reports: Trial Balance, Balance Sheet, Profit & Loss, General Ledger (all balance to the cent)
- ✅ Fiscal Periods: open / soft-close / lock / reopen; locked period rejects postings (423)
- ✅ Operational layer posting to the GL: Quick Sell POS (cash/card, discount, tax, COGS, barber commission, atomic stock decrement), Inventory (add/restock moving-avg/adjust), Invoices (issue/pay), Daily Cash Close (variance → ledger)
- ✅ Dashboard with 7 GL-derived tiles + recent entries
- ✅ Opening-balances migration (idempotent, equity plug) — run in seed
- ✅ Tested: backend 24/24 pytest, frontend critical flows 100%

## Backlog / future phases (from AUDIT_AND_PLAN)
- P1: Fiscal-year close (zero P&L into Retained Earnings), AR/AP aging reports, recurring journal entries
- P1: Gift cards / prepaid packages (2200), store-credit refunds (2210), Stripe merchant-fee auto-post (5500) from settlement
- P2: CSV bank import + reconciliation (2b), receipt uploads (2c), Plaid bank feeds (2d)
- P2: Appointments/booking/reminders (3a-3c), commission statements & booth rent (3e), chair profitability & cash-flow forecast (3f)

## Notes
- `/auth/switch-role` is an intentional demo convenience (no guard) — gate or remove for production.
- Non-owner manual entries are silently saved as draft (owner must post).
