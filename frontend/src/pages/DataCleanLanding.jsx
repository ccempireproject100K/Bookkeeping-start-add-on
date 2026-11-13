import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { animate, useInView } from "framer-motion";
import {
  Sparkles, Copy, ShieldCheck, Shield, Check, Gauge, ArrowRight, KeyRound, UploadCloud, Wand2,
} from "lucide-react";
import { api } from "@/lib/api";
import LanguageSwitcher from "@/components/LanguageSwitcher";
import { FALLBACK_PRICING, bundleComponentState, priceLabel, productState } from "@/lib/datacleanApi";

/** The bundle savings ticker (checkout-survey menu #3 — an animated
 * number, MagicUI-style): when the bundle card scrolls into view the
 * price counts DOWN from the parts total to the bundle price, so the
 * saving is something you watch happen, then the save badge lands.
 * Reduced-motion users get the final numbers immediately. */
function SavingsTicker({ bundle }) {
  const { t } = useTranslation();
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: "-40px" });
  const [cents, setCents] = useState(bundle.parts_total_cents);
  useEffect(() => {
    if (!inView) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) {
      setCents(bundle.monthly_price_cents);
      return;
    }
    const controls = animate(bundle.parts_total_cents, bundle.monthly_price_cents, {
      duration: 1.1, ease: "easeOut",
      onUpdate: (v) => setCents(Math.round(v)),
    });
    return () => controls.stop();
  }, [inView, bundle]);
  return (
    <span ref={ref} className="inline-flex flex-wrap items-center gap-2">
      <span>
        {t("datacleanLanding.bundleSeparately", { parts: priceLabel(bundle.parts_total_cents) })}
        {" → "}
        <span className="font-mono text-gray-200">{priceLabel(cents)}</span>
      </span>
      <span className="inline-block text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-full px-2.5 py-0.5 animate-shimmer motion-reduce:animate-none">
        {t("datacleanLanding.bundleSave", { amount: priceLabel(bundle.savings_cents) })}
      </span>
    </span>
  );
}

/** Scroll reveal (design pass 2026-11-11): the pricing halves rise in
 * as they enter view. Visible by default wherever IntersectionObserver
 * is unavailable or the user prefers reduced motion — content is never
 * gated on the animation. */
function Reveal({ children, delay = 0, className = "" }) {
  const ref = useRef(null);
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!("IntersectionObserver" in window)
      || window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) {
      setShown(true);
      return;
    }
    const io = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { setShown(true); io.disconnect(); }
    }, { threshold: 0.12 });
    io.observe(el);
    // Hard fallback: content must never stay hidden behind the reveal —
    // full-page captures, print, and odd embed contexts get it too.
    const fallback = setTimeout(() => setShown(true), 1200);
    return () => { io.disconnect(); clearTimeout(fallback); };
  }, []);
  return (
    <div ref={ref} style={{ transitionDelay: `${delay}ms` }}
      className={`reveal ${shown ? "reveal-in" : ""} ${className}`}>
      {children}
    </div>
  );
}

/** Per-half dynamic visual (design pass 2026-11-11) — pure CSS/SVG, no
 * image weight. Security: a shield under a sweeping scan line.
 * DataClean: messy, duplicated rows on the left resolving into aligned,
 * checked rows on the right. Decorative only. */
function HalfVisual({ productId }) {
  if (productId === "security") {
    return (
      <div aria-hidden="true" className="relative mt-5 h-32 rounded-xl overflow-hidden border border-cyan-400/25 bg-[radial-gradient(120px_80px_at_50%_45%,rgba(34,211,238,0.22),transparent_70%)]">
        <div className="absolute inset-0 flex items-center justify-center">
          <Shield size={76} strokeWidth={1.4} className="text-cyan-300 drop-shadow-[0_6px_16px_rgba(34,211,238,0.45)]" />
          <Check size={30} strokeWidth={3} className="absolute text-cyan-100" />
        </div>
        <div className="scan-line absolute left-[8%] right-[8%] h-[3px] rounded-full bg-cyan-300/80 shadow-[0_0_14px_3px_rgba(34,211,238,0.55)]" />
        <div className="absolute inset-x-6 bottom-2.5 flex justify-between">
          {[0, 1, 2, 3, 4].map((i) => (
            <span key={i} className="h-1 w-6 rounded-full bg-cyan-400/30" />
          ))}
        </div>
      </div>
    );
  }
  const messy = [
    { w: "72%", r: "-2deg", tone: "bg-gray-500/50" },
    { w: "88%", r: "1.5deg", tone: "bg-gray-500/40" },
    { w: "64%", r: "-1deg", tone: "bg-amber-400/50" },
    { w: "80%", r: "2deg", tone: "bg-gray-500/40" },
  ];
  const clean = ["92%", "92%", "92%", "92%"];
  return (
    <div aria-hidden="true" className="relative mt-5 h-32 rounded-xl overflow-hidden border border-amber-400/25 bg-[radial-gradient(120px_80px_at_50%_45%,rgba(245,158,11,0.18),transparent_70%)] px-4 py-3.5 flex items-center gap-2.5">
      <div className="flex-1 space-y-2">
        {messy.map((row, i) => (
          <div key={i} style={{ width: row.w, transform: `rotate(${row.r})` }}
            className={`h-3.5 rounded ${row.tone}`} />
        ))}
      </div>
      <ArrowRight size={18} className="shrink-0 text-amber-300" />
      <div className="flex-1 space-y-2">
        {clean.map((w, i) => (
          <div key={i} style={{ width: w }} className="row-shine flex items-center justify-end h-3.5 rounded bg-emerald-400/70 pr-1">
            <Check size={11} strokeWidth={3.5} className="text-emerald-950" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** Per-half accent language (design pass 2026-11-11): DataClean keeps
 * the house amber; Security takes a distinct cyan so the two halves
 * read as two products at a glance. */
const HALF_ACCENTS = {
  dataclean: {
    nameClass: "announce-amber",
    glow: "shadow-[0_24px_60px_-24px_rgba(245,158,11,0.45)]",
    bar: "from-amber-500 via-amber-300 to-amber-600",
    border: "hover:border-amber-400/50",
  },
  security: {
    nameClass: "announce-cyan",
    glow: "shadow-[0_24px_60px_-24px_rgba(34,211,238,0.42)]",
    bar: "from-cyan-500 via-cyan-300 to-cyan-600",
    border: "hover:border-cyan-400/50",
  },
};

/** DataClean storefront / pricing page (Phase 3 shell). Built into the
 * app like every other page; it goes live only when the whole stack
 * is deployed on the owner's word — nothing here is published
 * anywhere else. Prices render from the backend's pricing CONFIG
 * (GET /api/v1/dataclean/pricing) — the owner's stamped ladder
 * (2026-11-04): DataClean $12.99, Security $18.99, bundle $27.99
 * against $31.98 in parts. Availability is badged from the config's
 * own flags: DataClean sells today; Security is unbuilt, so it and
 * the bundle read coming soon and are never shown as purchasable. */
export default function DataCleanLanding() {
  const { t } = useTranslation();
  const [pricing, setPricing] = useState(FALLBACK_PRICING);

  useEffect(() => {
    document.title = `${t("datacleanLanding.name")} — Mind at Ease Solutions`;
    let live = true;
    api.get("/v1/dataclean/pricing")
      .then((p) => { if (live && p?.products) setPricing(p); })
      .catch(() => {});
    return () => { live = false; };
  }, [t]);

  const features = [
    { icon: Sparkles, title: t("datacleanLanding.f1Title"), body: t("datacleanLanding.f1Body") },
    { icon: Copy, title: t("datacleanLanding.f2Title"), body: t("datacleanLanding.f2Body") },
    { icon: Wand2, title: t("datacleanLanding.f3Title"), body: t("datacleanLanding.f3Body") },
    { icon: ShieldCheck, title: t("datacleanLanding.f4Title"), body: t("datacleanLanding.f4Body") },
  ];
  const steps = [
    { icon: KeyRound, title: t("datacleanLanding.s1Title"), body: t("datacleanLanding.s1Body") },
    { icon: UploadCloud, title: t("datacleanLanding.s2Title"), body: t("datacleanLanding.s2Body") },
    { icon: Gauge, title: t("datacleanLanding.s3Title"), body: t("datacleanLanding.s3Body") },
  ];

  return <div className="min-h-screen bg-[#0d0f14] text-gray-100 ledger-page">
    <header className="border-b border-[#1c212e]">
      <div className="max-w-5xl mx-auto px-5 py-4 flex items-center justify-between gap-4">
        <span className="font-display font-semibold tracking-tight">
          Mind at Ease <span className="text-amber-400">DataClean</span>
        </span>
        <div className="flex items-center gap-3">
          <Link to="/dataclean-demo" className="text-sm text-gray-400 hover:text-gray-100">{t("datacleanLanding.tryDemo")}</Link>
          <Link to="/login" className="text-sm text-gray-400 hover:text-gray-100">{t("datacleanLanding.signIn")}</Link>
          <LanguageSwitcher />
        </div>
      </div>
    </header>

    <main className="max-w-5xl mx-auto px-5 py-12 space-y-14">
      <section className="text-center space-y-5 animate-rise motion-reduce:animate-none">
        <h1 className="font-display text-4xl md:text-5xl font-semibold leading-tight">{t("datacleanLanding.heroTitle")}</h1>
        <p className="text-gray-400 max-w-2xl mx-auto">{t("datacleanLanding.heroBody")}</p>
        <div className="flex flex-wrap justify-center gap-3 pt-2">
          <Link to="/dataclean-demo" data-testid="landing-cta-demo"
            className="inline-flex items-center gap-2 h-11 px-5 rounded-lg bg-amber-500 text-black font-semibold text-sm hover:bg-amber-400 transition-colors motion-reduce:transition-none">
            {t("datacleanLanding.tryDemo")} <ArrowRight size={15} />
          </Link>
          <Link to="/login"
            className="inline-flex items-center gap-2 h-11 px-5 rounded-lg border border-[#2a3040] text-sm text-gray-200 hover:text-white transition-colors motion-reduce:transition-none">
            {t("datacleanLanding.openConsole")}
          </Link>
        </div>
      </section>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {features.map((f) => (
          <article key={f.title} className="card-surface p-6">
            <f.icon size={22} className="text-amber-400" />
            <h2 className="mt-3 font-display font-semibold">{f.title}</h2>
            <p className="mt-1.5 text-sm text-gray-400">{f.body}</p>
          </article>))}
      </section>

      <section>
        <h2 className="font-display text-2xl font-semibold text-center">{t("datacleanLanding.howTitle")}</h2>
        <div className="mt-6 grid grid-cols-1 md:grid-cols-3 gap-4">
          {steps.map((s, i) => (
            <article key={s.title} className="card-surface p-6">
              <div className="flex items-center gap-3">
                <span className="font-mono text-amber-400 text-lg">{i + 1}</span>
                <s.icon size={20} className="text-amber-400" />
              </div>
              <h3 className="mt-3 font-display font-semibold">{s.title}</h3>
              <p className="mt-1.5 text-sm text-gray-400">{s.body}</p>
            </article>))}
        </div>
      </section>

      <section>
        <h2 className="font-display text-2xl font-semibold text-center">{t("datacleanLanding.pricingTitle")}</h2>
        {pricing.placeholder && (
          <p className="mt-3 text-center" data-testid="landing-placeholder-badge">
            <span className="inline-block text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-full px-3 py-1.5">
              {t("datacleanLanding.placeholderNote")}
            </span>
          </p>)}
        <div className="tilt-scene mt-8 grid grid-cols-1 md:grid-cols-2 gap-5 max-w-4xl mx-auto">
          {(pricing.products || []).map((p, i) => {
            const accent = HALF_ACCENTS[p.id] || HALF_ACCENTS.dataclean;
            return (
            <Reveal key={p.id} delay={i * 120} className="h-full">
            <article className={`card-surface tilt-card relative overflow-hidden p-6 pt-7 flex flex-col h-full ${accent.glow} ${accent.border}`} data-testid={`landing-plan-${p.id}`}>
              <div aria-hidden="true" className={`absolute inset-x-0 top-0 h-1.5 bg-gradient-to-r ${accent.bar}`} />
              <div className="flex items-center justify-end gap-3">
                {productState(p) === "available" ? (
                  <span className="inline-block text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-full px-2.5 py-1">
                    {t("datacleanLanding.statusAvailableNow")}
                  </span>) : (
                  <span className="inline-block text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-full px-2.5 py-1">
                    {t("datacleanLanding.statusComingSoon")}
                  </span>)}
              </div>
              <h3 className={`announce-3d announce-float mt-3 font-display font-black uppercase leading-none text-4xl xl:text-5xl ${accent.nameClass}`}>
                {p.name}
              </h3>
              <HalfVisual productId={p.id} />
              <div className="mt-5 font-mono text-4xl font-semibold">
                {priceLabel(p.monthly_price_cents)}
                <span className="text-sm text-gray-500"> {t("datacleanLanding.perMonth")}</span>
              </div>
              <p className="mt-4 text-sm text-gray-400 flex-1">
                {t(p.id === "security" ? "datacleanLanding.productSecurityBody" : "datacleanLanding.productDatacleanBody")}
              </p>
              {productState(p) === "available" ? (
                <Link to="/dataclean-checkout"
                  className="mt-5 inline-flex justify-center items-center gap-2 h-10 px-4 rounded-lg bg-amber-500 text-black font-semibold text-sm hover:bg-amber-400 transition-colors motion-reduce:transition-none">
                  {t("datacleanLanding.subscribe")} <ArrowRight size={15} />
                </Link>) : (
                <span className="mt-5 inline-flex justify-center items-center h-10 px-4 rounded-lg border border-[#2a3040] text-sm text-gray-500">
                  {t("datacleanLanding.statusComingSoon")}
                </span>)}
            </article>
            </Reveal>);
          })}
        </div>
        {(pricing.bundles || []).map((b) => (
          <Reveal key={b.id} className="mt-4">
          <article className="card-surface p-6 relative overflow-hidden border-amber-500/40 shadow-[0_24px_60px_-24px_rgba(245,158,11,0.35)]" data-testid={`landing-bundle-${b.id}`}>
            <div aria-hidden="true" className="absolute inset-x-0 top-0 h-1.5 bg-gradient-to-r from-amber-500 via-cyan-400 to-cyan-600" />
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <h3 className="font-display text-2xl font-extrabold tracking-tight flex items-center gap-2">
                {b.name}
                <span className="inline-block text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-full px-2.5 py-1">
                  {t("datacleanLanding.bestValue")}
                </span>
              </h3>
              <div className="text-right">
                <div className="font-mono text-3xl font-semibold">
                  {priceLabel(b.monthly_price_cents)}
                  <span className="text-sm text-gray-500"> {t("datacleanLanding.perMonth")}</span>
                </div>
                {b.savings_cents > 0 && (
                  <p className="mt-1 text-xs text-gray-500">
                    <SavingsTicker bundle={b} />
                  </p>)}
              </div>
            </div>
            <p className="mt-2 text-sm text-gray-400">{t("datacleanLanding.bundleBody")}</p>
            <ul className="mt-4 space-y-2 text-sm">
              {(b.components || []).map((c) => (
                <li key={c.id} className="flex items-center justify-between gap-3">
                  <span className="text-gray-200">{c.name}</span>
                  {bundleComponentState(c) === "available" ? (
                    <span className="inline-block text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-full px-2.5 py-1">
                      {t("datacleanLanding.statusAvailableNow")}
                    </span>) : (
                    <span className="inline-block text-xs text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-full px-2.5 py-1">
                      {t("datacleanLanding.statusComingSoon")}
                    </span>)}
                </li>))}
            </ul>
            {b.sellable ? (
              <Link to="/dataclean-checkout"
                className="mt-5 inline-flex justify-center items-center gap-2 h-10 px-4 rounded-lg bg-amber-500 text-black font-semibold text-sm hover:bg-amber-400 transition-colors motion-reduce:transition-none">
                {t("datacleanLanding.subscribe")} <ArrowRight size={15} />
              </Link>) : (
              <span className="mt-5 inline-flex justify-center items-center h-10 px-4 rounded-lg border border-[#2a3040] text-sm text-gray-500">
                {t("datacleanLanding.statusComingSoon")}
              </span>)}
          </article>
          </Reveal>))}
        {pricing.billing_mode === "test" && (
          <p className="mt-4 text-center text-xs text-gray-600">{t("datacleanLanding.testModeNote")}</p>)}
      </section>

      <footer className="border-t border-[#1c212e] pt-6 pb-2 text-center text-xs text-gray-600">
        {t("datacleanLanding.footer")}
      </footer>
    </main>
  </div>;
}
