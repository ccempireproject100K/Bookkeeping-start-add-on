import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { loadStripe } from "@stripe/stripe-js";
import { EmbeddedCheckoutProvider, EmbeddedCheckout } from "@stripe/react-stripe-js";
import confetti from "canvas-confetti";
import lottie from "lottie-web";
import {
  Sparkles, ShieldCheck, Lock, CalendarX2, Database, Check, Wand2, RefreshCw,
} from "lucide-react";
import { api, errMsg } from "@/lib/api";
import { priceLabel } from "@/lib/datacleanApi";
import successRings from "@/lib/successRings.json";

/** The magical checkout (owner's word) for the DataClean family —
 * built on the checkout survey's ADOPT picks (see
 * frontend/PROVENANCE.md): Stripe's embedded checkout
 * (@stripe/react-stripe-js, ui_mode "elements") renders the payment
 * step INSIDE this page when a publishable key is configured, with a
 * hosted-Checkout redirect as the fallback; Motion drives the plan
 * morphing and the animated bundle-savings ticker; canvas-confetti
 * + a hand-authored Lottie medallion carry the success moment.
 *
 * Honesty rules hold here exactly as on the backend: prices and
 * sellable flags come from GET /dataclean/billing/status (the
 * stamped config), Security and the bundle are visible but NOT
 * purchasable until the backend says sellable, and when billing is
 * not configured the page says so instead of faking a form. Access
 * itself is granted by the webhook, never by arriving on this page —
 * the success view says that plainly and polls for the entitlement.
 */

const BLURB_KEYS = {
  dataclean: "datacleanCheckout.datacleanBlurb",
  security: "datacleanCheckout.securityBlurb",
  bundle: "datacleanCheckout.bundleBlurb",
};
const ICONS = { dataclean: Sparkles, security: ShieldCheck, bundle: Wand2 };
const BRAND_COLORS = ["#f59e0b", "#34d399", "#fbbf24", "#fde68a"];

let _stripePromise = null;
let _stripeKey = "";
function stripeFor(publishableKey) {
  if (!publishableKey) return null;
  if (_stripeKey !== publishableKey) {
    _stripeKey = publishableKey;
    _stripePromise = loadStripe(publishableKey);
  }
  return _stripePromise;
}

/** Animated $31.98 → $27.99 count-down for the bundle card. */
function SavingsTicker({ partsCents, priceCents, savingsCents }) {
  const { t } = useTranslation();
  const [cents, setCents] = useState(partsCents);
  useEffect(() => {
    setCents(partsCents);
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) {
      setCents(priceCents);
      return;
    }
    let raf = 0;
    const started = performance.now();
    const tick = (now) => {
      const p = Math.min(1, (now - started) / 1100);
      setCents(Math.round(partsCents + (priceCents - partsCents) * (1 - Math.pow(1 - p, 3))));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [partsCents, priceCents]);
  return (
    <span className="inline-flex flex-wrap items-center gap-2 text-xs">
      <span className="text-gray-500 line-through">{priceLabel(partsCents)}</span>
      <span aria-hidden="true" className="text-gray-500">→</span>
      <span className="font-mono text-gray-100">{priceLabel(cents)}</span>
      <span className="inline-block text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-full px-2.5 py-0.5 animate-shimmer motion-reduce:animate-none">
        {t("datacleanCheckout.bundleSave", { amount: priceLabel(savingsCents) })}
      </span>
    </span>
  );
}

function Stepper({ step }) {
  const { t } = useTranslation();
  const labels = [t("datacleanCheckout.stepPlan"), t("datacleanCheckout.stepPay"), t("datacleanCheckout.stepDone")];
  return (
    <ol className="flex items-center justify-center gap-2 text-xs" aria-label="checkout progress">
      {labels.map((label, i) => (
        <li key={label} className="flex items-center gap-2">
          <span className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 border ${
            i < step
              ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
              : i === step
                ? "border-amber-500/50 text-amber-300 bg-amber-500/10"
                : "border-[#2a3040] text-gray-500"}`}>
            {i < step ? <Check size={12} /> : <span className="font-mono">{i + 1}</span>}
            {label}
          </span>
          {i < labels.length - 1 && <span className="w-4 h-px bg-[#2a3040]" aria-hidden="true" />}
        </li>
      ))}
    </ol>
  );
}

function SuccessMedallion() {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return;
    let anim;
    try {
      anim = lottie.loadAnimation({
        container: ref.current, renderer: "svg",
        loop: false, autoplay: true, animationData: successRings,
      });
    } catch { /* the static medallion below still carries the moment */ }
    return () => { try { anim?.destroy(); } catch { /* noop */ } };
  }, []);
  return (
    <div className="relative w-44 h-44 mx-auto" aria-hidden="true">
      <div ref={ref} className="absolute inset-0" />
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-emerald-500/15 border border-emerald-400/50">
          <Check size={30} className="text-emerald-300" />
        </span>
      </div>
    </div>
  );
}

export default function DataCleanCheckout() {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const outcome = params.get("checkout"); // "success" | "cancelled" | null
  const [status, setStatus] = useState(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [selected, setSelected] = useState("dataclean");
  const [starting, setStarting] = useState(false);
  const [payError, setPayError] = useState("");
  const [payReady, setPayReady] = useState(false);

  const loadStatus = useCallback(() => {
    return api.get("/dataclean/billing/status")
      .then((s) => { setStatus(s); return s; })
      .catch(() => { setLoadFailed(true); return null; });
  }, []);

  useEffect(() => { loadStatus(); }, [loadStatus]);

  // Success: celebrate once, then poll briefly for the webhook-driven
  // entitlement (access is granted by the webhook, not by this page).
  const celebrated = useRef(false);
  useEffect(() => {
    if (outcome !== "success" || celebrated.current) return;
    celebrated.current = true;
    if (!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) {
      const fire = (x, delay) => setTimeout(() => {
        try {
          confetti({ particleCount: 90, spread: 75, startVelocity: 38,
                     origin: { x, y: 0.25 }, colors: BRAND_COLORS, zIndex: 60 });
        } catch { /* confetti is garnish, never a blocker */ }
      }, delay);
      fire(0.25, 150); fire(0.75, 400);
    }
    let tries = 0;
    const iv = setInterval(async () => {
      tries += 1;
      const s = await loadStatus();
      if (s?.billing?.entitled || tries >= 6) clearInterval(iv);
    }, 3000);
    return () => clearInterval(iv);
  }, [outcome, loadStatus]);

  const catalog = status?.catalog || [];
  const chosen = catalog.find((c) => c.id === selected) || catalog[0];
  const stripePromise = useMemo(
    () => stripeFor(status?.publishable_key), [status?.publishable_key]);

  const fetchClientSecret = useCallback(async () => {
    const out = await api.post("/dataclean/billing/checkout", {
      product: "dataclean",
      origin_url: window.location.origin,
      ui_mode: "elements",
    });
    if (!out?.client_secret) throw new Error("no client secret");
    return out.client_secret;
  }, []);

  const startHosted = async () => {
    setStarting(true); setPayError("");
    try {
      const out = await api.post("/dataclean/billing/checkout", {
        product: chosen.id,
        origin_url: window.location.origin,
        ui_mode: "hosted",
      });
      if (out?.checkout_url) { window.location.assign(out.checkout_url); return; }
      throw new Error("no checkout url");
    } catch (e) {
      setPayError(errMsg(e) || t("datacleanCheckout.payError"));
      setStarting(false);
    }
  };

  const header = (
    <div className="text-center space-y-3">
      <h1 className="font-display text-3xl md:text-4xl font-semibold flex items-center justify-center gap-2">
        <Sparkles className="text-amber-400" size={26} />
        {t("datacleanCheckout.title")}
      </h1>
      <p className="text-gray-400 max-w-xl mx-auto text-sm">{t("datacleanCheckout.subtitle")}</p>
      {status?.billing_mode === "test" && (
        <span className="inline-block text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-full px-3 py-1">
          {t("datacleanCheckout.testMode")}
        </span>
      )}
    </div>
  );

  if (outcome === "success") {
    const entitled = !!status?.billing?.entitled;
    return (
      <div className="max-w-2xl mx-auto space-y-6 py-4" data-testid="checkout-success">
        {header}
        <Stepper step={2} />
        <section className="card-surface p-8 text-center space-y-4 animate-rise motion-reduce:animate-none">
          <SuccessMedallion />
          <h2 className="font-display text-2xl font-semibold">{t("datacleanCheckout.successTitle")}</h2>
          <p className="text-sm text-gray-400">{t("datacleanCheckout.successBody")}</p>
          {entitled ? (
            <p className="text-sm text-emerald-300" data-testid="checkout-success-active">
              {t("datacleanCheckout.successActive")}
            </p>
          ) : (
            <p className="text-sm text-gray-400">{t("datacleanCheckout.successWaiting")}</p>
          )}
          <div className="flex flex-wrap justify-center gap-3 pt-2">
            <Link to="/dataclean-api"
              className="inline-flex items-center gap-2 h-11 px-5 rounded-lg bg-amber-500 text-black font-semibold text-sm hover:bg-amber-400 transition-colors motion-reduce:transition-none">
              {t("datacleanCheckout.openConsole")}
            </Link>
            <button type="button" onClick={loadStatus}
              className="inline-flex items-center gap-2 h-11 px-5 rounded-lg border border-[#2a3040] text-sm text-gray-200 hover:text-white transition-colors motion-reduce:transition-none">
              <RefreshCw size={15} /> {t("datacleanCheckout.refresh")}
            </button>
          </div>
        </section>
        {/* The simple self-built post-purchase step (the survey found
            no permissive order-bump component exists): an honest
            heads-up about the bundle — informational, never a charge. */}
        <section className="card-surface p-6 space-y-2" data-testid="checkout-bundle-teaser">
          <h3 className="font-display font-semibold flex items-center gap-2">
            <Wand2 size={17} className="text-amber-400" />
            {t("datacleanCheckout.bundleTeaserTitle")}
          </h3>
          <p className="text-sm text-gray-400">
            {t("datacleanCheckout.bundleTeaserBody", {
              amount: priceLabel(catalog.find((c) => c.id === "bundle")?.savings_cents ?? 399),
            })}
          </p>
        </section>
      </div>
    );
  }

  if (outcome === "cancelled") {
    return (
      <div className="max-w-2xl mx-auto space-y-6 py-4" data-testid="checkout-cancelled">
        {header}
        <section className="card-surface p-8 text-center space-y-4">
          <h2 className="font-display text-2xl font-semibold">{t("datacleanCheckout.cancelledTitle")}</h2>
          <p className="text-sm text-gray-400">{t("datacleanCheckout.cancelledBody")}</p>
          <button type="button" onClick={() => navigate("/dataclean-checkout")}
            className="inline-flex items-center gap-2 h-11 px-5 rounded-lg bg-amber-500 text-black font-semibold text-sm hover:bg-amber-400 transition-colors motion-reduce:transition-none">
            {t("datacleanCheckout.backToPlans")}
          </button>
        </section>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6 py-4">
      {header}
      <Stepper step={payReady ? 1 : 0} />

      {!status && !loadFailed && (
        <p className="text-center text-sm text-gray-500 animate-pulse" data-testid="checkout-loading">
          {t("datacleanCheckout.loading")}
        </p>
      )}
      {(loadFailed || (status && !status.configured)) && (
        <section className="card-surface p-8 text-center space-y-3" data-testid="checkout-not-configured">
          <ShieldCheck size={26} className="mx-auto text-amber-400" />
          <h2 className="font-display text-xl font-semibold">{t("datacleanCheckout.notConfiguredTitle")}</h2>
          <p className="text-sm text-gray-400 max-w-lg mx-auto">{t("datacleanCheckout.notConfiguredBody")}</p>
        </section>
      )}

      {status?.configured && status.billing?.entitled && (
        <section className="card-surface p-8 text-center space-y-4" data-testid="checkout-active">
          <span className="inline-flex items-center justify-center w-14 h-14 rounded-full bg-emerald-500/15 border border-emerald-400/50">
            <Check size={26} className="text-emerald-300" />
          </span>
          <h2 className="font-display text-xl font-semibold">{t("datacleanCheckout.activeTitle")}</h2>
          <p className="text-sm text-gray-400">
            {t("datacleanCheckout.activeBody", { status: status.billing.status || "active" })}
          </p>
          <div className="flex flex-wrap justify-center gap-3">
            <Link to="/dataclean-api"
              className="inline-flex items-center gap-2 h-11 px-5 rounded-lg bg-amber-500 text-black font-semibold text-sm hover:bg-amber-400 transition-colors motion-reduce:transition-none">
              {t("datacleanCheckout.openConsole")}
            </Link>
            <Link to="/dataclean-studio"
              className="inline-flex items-center gap-2 h-11 px-5 rounded-lg border border-[#2a3040] text-sm text-gray-200 hover:text-white transition-colors motion-reduce:transition-none">
              {t("datacleanCheckout.openStudio")}
            </Link>
          </div>
        </section>
      )}

      {status?.configured && !status.billing?.entitled && catalog.length > 0 && (
        <>
          <section className="grid grid-cols-1 md:grid-cols-3 gap-4" role="radiogroup"
                   aria-label={t("datacleanCheckout.stepPlan")}>
            {catalog.map((c) => {
              const Icon = ICONS[c.id] || Sparkles;
              const isChosen = chosen?.id === c.id;
              return (
                <motion.article key={c.id} layout
                  transition={{ type: "spring", stiffness: 320, damping: 30 }}
                  className={`card-surface p-5 flex flex-col gap-3 relative ${
                    c.id === "bundle" ? "border-amber-500/40" : ""} ${
                    isChosen ? "ring-2 ring-amber-400/70" : ""}`}
                  data-testid={`checkout-plan-${c.id}`}>
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="font-display font-semibold flex items-center gap-2">
                      <Icon size={17} className="text-amber-400" /> {c.name}
                    </h3>
                    {c.sellable ? (
                      <span className="inline-block text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-full px-2.5 py-0.5">
                        {t("datacleanCheckout.availableNow")}
                      </span>
                    ) : (
                      <span className="inline-block text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-full px-2.5 py-0.5">
                        {t("datacleanCheckout.comingSoon")}
                      </span>
                    )}
                  </div>
                  <div className="font-mono text-3xl font-semibold">
                    <AnimatePresence mode="popLayout" initial={false}>
                      <motion.span key={c.id + String(isChosen)}
                        initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.18 }}
                        className="inline-block">
                        {priceLabel(c.amount_cents)}
                      </motion.span>
                    </AnimatePresence>
                    <span className="text-sm text-gray-500"> {t("datacleanCheckout.perMonth")}</span>
                  </div>
                  {c.id === "bundle" && c.savings_cents > 0 && (
                    <SavingsTicker partsCents={c.parts_total_cents}
                                   priceCents={c.amount_cents}
                                   savingsCents={c.savings_cents} />
                  )}
                  <p className="text-sm text-gray-400 flex-1">{t(BLURB_KEYS[c.id] || BLURB_KEYS.dataclean)}</p>
                  <button type="button" role="radio" aria-checked={isChosen}
                    disabled={!c.sellable}
                    onClick={() => { setSelected(c.id); setPayError(""); }}
                    data-testid={`checkout-select-${c.id}`}
                    className={`h-10 rounded-lg text-sm font-semibold transition-colors motion-reduce:transition-none ${
                      !c.sellable
                        ? "border border-[#2a3040] text-gray-500 cursor-not-allowed"
                        : isChosen
                          ? "bg-amber-500 text-black"
                          : "border border-amber-500/40 text-amber-300 hover:bg-amber-500/10"}`}>
                    {!c.sellable
                      ? t("datacleanCheckout.comingSoon")
                      : isChosen
                        ? t("datacleanCheckout.selected")
                        : t("datacleanCheckout.stepPlan")}
                  </button>
                </motion.article>
              );
            })}
          </section>

          {chosen?.sellable && (
            <section className="card-surface p-6 space-y-4" data-testid="checkout-pay">
              {stripePromise ? (
                <EmbeddedCheckoutProvider stripe={stripePromise}
                                          options={{ fetchClientSecret, onComplete: () => setPayReady(true) }}>
                  <EmbeddedCheckout />
                </EmbeddedCheckoutProvider>
              ) : (
                <div className="text-center space-y-3">
                  <button type="button" onClick={startHosted} disabled={starting}
                    className="inline-flex items-center gap-2 h-11 px-6 rounded-lg bg-amber-500 text-black font-semibold text-sm hover:bg-amber-400 transition-colors motion-reduce:transition-none disabled:opacity-60">
                    <Lock size={15} />
                    {starting ? t("datacleanCheckout.starting") : t("datacleanCheckout.payNow")}
                  </button>
                </div>
              )}
              {payError && <p className="text-sm text-red-300 text-center" role="alert">{payError}</p>}
              <ul className="flex flex-wrap justify-center gap-x-6 gap-y-2 text-xs text-gray-500 pt-1">
                <li className="inline-flex items-center gap-1.5"><Lock size={13} /> {t("datacleanCheckout.trustStripe")}</li>
                <li className="inline-flex items-center gap-1.5"><CalendarX2 size={13} /> {t("datacleanCheckout.trustCancel")}</li>
                <li className="inline-flex items-center gap-1.5"><Database size={13} /> {t("datacleanCheckout.trustData")}</li>
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}
