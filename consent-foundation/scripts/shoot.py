#!/usr/bin/env python3
"""Capture proof screenshots of the staged UI surfaces.

Serves the ui/ folder isn't needed — the pages are self-contained, so we
open them by file:// URL in headless Chromium (Playwright).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)

from playwright.sync_api import sync_playwright  # noqa: E402

PAGES = [
    ("onboarding_step.html", "onboarding-step.png"),
    ("settings_privacy.html", "settings-privacy.png"),
    ("cohort_report.html", "cohort-report.png"),
    ("coverage_report.html", "coverage-report.png"),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 720, "height": 1100})
    for src, out in PAGES:
        page.goto((ROOT / "ui" / src).as_uri())
        page.wait_for_timeout(400)
        if src == "onboarding_step.html":
            # The improvement-pass proof shows both unchecked consent boxes
            # with the "What this means" panel expanded.
            page.eval_on_selector("#what-this-means", "el => { el.open = true; }")
            page.wait_for_timeout(200)
        page.screenshot(path=str(SHOTS / out), full_page=True)
        print("shot:", out)
        if src == "onboarding_step.html":
            page.screenshot(
                path=str(SHOTS / "onboarding-split-consents-panel-open.png"),
                full_page=True,
            )
            print("shot: onboarding-split-consents-panel-open.png")
        if src == "settings_privacy.html":
            page.screenshot(path=str(SHOTS / "contributor-receipt.png"), full_page=True)
            print("shot: contributor-receipt.png")
    # Onboarding interaction proof: terms accepted, both optional clauses
    # left unchecked, account still creates and both records show DECLINED.
    page.goto((ROOT / "ui" / "onboarding_step.html").as_uri())
    page.check("#terms")
    page.click("#submit")
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / "onboarding-terms-only-declined.png"), full_page=True)
    print("shot: onboarding-terms-only-declined.png")
    # Nudge proof: terms accepted + Scope A CHECKED + Scope B UNCHECKED,
    # then Continue — the one-time pop-up for Scope B only appears, with
    # the owner's respect line visible above two equal buttons.
    page.goto((ROOT / "ui" / "onboarding_step.html").as_uri())
    page.check("#terms")
    page.check("#datarelease")
    page.click("#submit")
    page.wait_for_selector("#nudge-overlay.open")
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / "nudge-scope-b.png"))
    print("shot: nudge-scope-b.png")
    browser.close()
print("done")
