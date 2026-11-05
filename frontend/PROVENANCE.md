# Frontend PROVENANCE — DataClean checkout build (2026-11-04)

Adoptions for the DataClean magical checkout, screened in
`~/workspace/buildout/checkout-survey/REPORT.md` (licenses verified
from each repo's own LICENSE file on 2026-11-04). Only permissive
licenses (MIT / ISC) are used; anything copied or installed carries
its entry here per the house skills-in-every-repo rule.

## Installed packages (ADOPT)

| Package | License | Source | Used for |
|---|---|---|---|
| `@stripe/stripe-js` | MIT | github.com/stripe/stripe-js (official) | Loading Stripe.js with the publishable key from the billing status endpoint |
| `@stripe/react-stripe-js` | MIT | github.com/stripe/react-stripe-js (official) | Embedded checkout — Checkout Sessions with `ui_mode: "elements"` rendered inside `pages/DataCleanCheckout.jsx`; the payment step never leaves our page |
| `canvas-confetti` | ISC | github.com/catdad/canvas-confetti | The success-moment confetti burst (brand colors) |
| `lottie-web` | MIT | github.com/airbnb/lottie-web | Player for the success medallion animation |
| `framer-motion` | MIT | github.com/motiondivision/motion (already in this tree before the build; the survey's "Motion" is this library's successor line) | Plan-card layout morphing, price transitions, the animated bundle-savings ticker |

## Original assets

- `src/lib/successRings.json` — the Lottie success animation (expanding
  rings + sparks) was authored for this repo in this build. No
  third-party animation asset is bundled.

## Patterns adapted — method only, no code copied

- **Checkout → webhook → entitlement sync** (survey menu #10): the
  flow architecture proven by vercel/nextjs-subscription-payments
  (MIT, archived — pattern reference only) and wasp-lang/open-saas
  (MIT), re-implemented from scratch in FastAPI
  (`backend/lib/dataclean_billing.py`, `backend/routers/dataclean_billing.py`)
  against this repo's own tables.
- **Session/webhook call patterns**: stripe-samples'
  checkout-one-time-payments Python server (MIT) as the reference
  shape; our endpoints are original FastAPI code.
- **Bundle-as-hero pricing** (survey menu #7): the highlighted-plan
  pattern from scosman/CMSaasStarter (MIT) and revokslab/ShipFree
  (MIT), applied to our own landing/checkout components.
- **Animated number / savings ticker** (survey menu #3): the pattern
  popularized by magicuidesign/magicui's registry (MIT); implemented
  locally with framer-motion / requestAnimationFrame — no MagicUI
  code copied.
- **shadcn/ui skeleton** (MIT): this repo already carries the
  shadcn-style component set under `src/components/ui`; the checkout
  reuses the house card/button conventions instead of importing new
  components.

## Self-built (survey honesty note)

- The post-purchase bundle teaser on the success view is an original,
  informational step built for this repo. The survey found no
  permissive-license order-bump / exit-save component in the wild;
  nothing of that kind was copied, and the teaser never charges or
  pre-checks anything.
- The stepped progress indicator and trust-badge row are original
  markup on the house component conventions.

## Explicitly NOT used (per the survey)

- ixartz/SaaS-Boilerplate — its Stripe code is paywalled upstream.
- getlago/lago — AGPL; concepts only, nothing copied.
- polarsource/polar — would replace the Stripe rail; not adopted.
