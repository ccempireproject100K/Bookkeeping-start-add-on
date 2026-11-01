import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Sparkles, Copy, ShieldCheck, Gauge, ArrowRight, KeyRound, UploadCloud, Wand2,
} from "lucide-react";
import { api } from "@/lib/api";
import LanguageSwitcher from "@/components/LanguageSwitcher";
import { FALLBACK_PRICING, bundleComponentState, priceLabel } from "@/lib/datacleanApi";
import { num as _n } from "@/lib/format";

/** DataClean storefront / pricing page (Phase 3 shell). Built into the
 * app like every other page; it goes live only when the whole stack
 * is deployed on the owner's word — nothing here is published
 * anywhere else. Prices render from the backend's pricing CONFIG
 * (GET /api/v1/dataclean/pricing); the plan prices are still
 * placeholders and this page badges them exactly that way rather
 * than dressing them up as final. The bundle below the plans is the
 * exception: its price is owner-set (2026-10-31), and each half is
 * badged from the config's own flags — DataClean available now, the
 * security half coming soon and never shown as available. */
export default function DataCleanLanding() {
  const { t } = useTranslation();
  const [pricing, setPricing] = useState(FALLBACK_PRICING);

  useEffect(() => {
    document.title = `${t("datacleanLanding.name")} — Mind at Ease Solutions`;
    let live = true;
    api.get("/v1/dataclean/pricing")
      .then((p) => { if (live && p?.plans) setPricing(p); })
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

  return <div className="min-h-screen bg-[#0d0f14] text-gray-100">
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
        <div className="mt-6 grid grid-cols-1 md:grid-cols-3 gap-4">
          {(pricing.plans || []).map((p) => (
            <article key={p.id} className="card-surface p-6 flex flex-col" data-testid={`landing-plan-${p.id}`}>
              <h3 className="font-display font-semibold">{p.name}</h3>
              <div className="mt-3 font-mono text-3xl font-semibold">
                {priceLabel(p.monthly_price_cents)}
                <span className="text-sm text-gray-500"> {t("datacleanLanding.perMonth")}</span>
              </div>
              <ul className="mt-4 space-y-2 text-sm text-gray-400 flex-1">
                <li>{t("datacleanLanding.rowsIncluded", { rows: _n(p.rows_included) })}</li>
                <li>{t("datacleanLanding.overage", { price: priceLabel(p.overage_per_1000_rows_cents) })}</li>
              </ul>
              <Link to="/dataclean-demo"
                className="mt-5 inline-flex justify-center items-center gap-2 h-10 px-4 rounded-lg border border-amber-500/40 text-amber-300 text-sm hover:bg-amber-500/10 transition-colors motion-reduce:transition-none">
                {t("datacleanLanding.tryDemo")}
              </Link>
            </article>))}
        </div>
        {(pricing.bundles || []).map((b) => (
          <article key={b.id} className="card-surface p-6 mt-4" data-testid={`landing-bundle-${b.id}`}>
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <h3 className="font-display font-semibold">{b.name}</h3>
              <div className="font-mono text-3xl font-semibold">
                {priceLabel(b.monthly_price_cents)}
                <span className="text-sm text-gray-500"> {t("datacleanLanding.perMonth")}</span>
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
          </article>))}
        <p className="mt-4 text-center text-xs text-gray-600">{t("datacleanLanding.testModeNote")}</p>
      </section>

      <footer className="border-t border-[#1c212e] pt-6 pb-2 text-center text-xs text-gray-600">
        {t("datacleanLanding.footer")}
      </footer>
    </main>
  </div>;
}
