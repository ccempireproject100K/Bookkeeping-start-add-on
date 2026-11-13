"""Data-release consent module (staged buildout).

Contents:
- clause.py      — independently versioned Scope A and Scope B clauses
- gate.py        — onboarding gate: both consent checkboxes are independent
- store.py       — per-scope consent records + append-only change log (SQLite)
- cohorts.py     — per-scope, current-version opt-in / opt-out rates
- suppression.py — re-identification cell check gating any future data release
- nudge.py       — the one-time onboarding pop-up: informs, never manufactures consent
- coverage.py    — presentable coverage statement: counts/rates only, floor-suppressed
"""
