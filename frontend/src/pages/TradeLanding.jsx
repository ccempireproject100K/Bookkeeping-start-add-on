import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, CheckCircle2 } from "lucide-react";
import { api } from "@/lib/api";
import { findPack } from "@/lib/packs";
import { asSkin, readMarketChoice, writeMarketChoice } from "@/lib/markets";
import FaceIcon from "@/components/FaceIcon";
import MarketSelect from "@/components/MarketSelect";
import LanguageSwitcher from "@/components/LanguageSwitcher";

/** Public landing page for one trade edition, rendered entirely from its
 *  niche pack (GET /niches): product name, tagline, terminology, services,
 *  checklist and visual identity. All 33 trades get this page from the one
 *  component — there are no hand-written per-trade pages.
 *  Choosing a market re-skins the page with the composed trade x market
 *  face from GET /market-packs/resolve (market accent wins, flag +
 *  languages + currency from the market) — the same composition the
 *  backend resolver serves. The choice persists (localStorage), shared
 *  with /industries and login. */
export default function TradeLanding() {
  const { nicheId } = useParams();
  const [pack, setPack] = useState(null);
  const [missing, setMissing] = useState(false);
  const [marketCode, setMarketCode] = useState(() => readMarketChoice());
  const [skin, setSkin] = useState(null);

  useEffect(() => {
    let live = true;
    api.get("/niches")
      .then((packs) => {
        if (!live) return;
        const found = findPack(packs, nicheId);
        if (found) {
          setPack(found);
          document.title = `${found.product_name} — Bookkeeping for ${found.trade} | Mind at Ease Solutions`;
        } else {
          setMissing(true);
          document.title = "Industry not found — Mind at Ease Solutions";
        }
      })
      .catch(() => live && setMissing(true));
    return () => { live = false; };
  }, [nicheId]);

  useEffect(() => {
    if (!marketCode) { setSkin(null); return; }
    let live = true;
    api.get("/market-packs/resolve", { params: { trade: nicheId, market: marketCode } })
      .then((data) => { if (live) setSkin(asSkin(data)); })
      .catch(() => { if (live) setSkin(null); });
    return () => { live = false; };
  }, [nicheId, marketCode]);

  const pickMarket = (code) => {
    writeMarketChoice(code);
    setMarketCode(code);
  };

  if (missing) {
    return (
      <div className="min-h-screen bg-[#0B0D10] text-gray-100 flex items-center justify-center p-6">
        <div className="text-center">
          <h1 className="font-display text-3xl font-bold mb-3">That industry isn't on the list yet</h1>
          <p className="text-gray-400 mb-6">Mind at Ease Solutions covers 33 trades — yours may be one of them.</p>
          <Link to="/industries" className="text-amber-500 hover:text-amber-400 text-sm">
            <ArrowLeft size={14} className="inline me-1 rtl:-scale-x-100" /> See all industries
          </Link>
        </div>
      </div>
    );
  }

  if (!pack) {
    return (
      <div className="min-h-screen bg-[#0B0D10] text-gray-100 flex items-center justify-center">
        <p className="text-gray-500 text-sm">Loading…</p>
      </div>
    );
  }

  const accent = skin?.accent || pack.colors?.accent || "#f59e0b";
  const market = skin?.market || null;
  const terms = pack.terms || {};
  const worker = (terms.worker_plural || "Staff").toLowerCase();
  const job = terms.job || "job";
  const customer = terms.customer || "customer";

  const features = [
    {
      title: `Job costing on every ${job}`,
      body: `Materials, labor, equipment and subs tracked live against each ${job}, with estimate-vs-actual profit the moment it updates.`,
    },
    {
      title: `Pay ${worker} without the spreadsheet`,
      body: `Payroll, commissions and payouts split automatically per ${terms.worker?.toLowerCase() || "team member"} — no end-of-week arithmetic.`,
    },
    {
      title: `A ledger that speaks ${pack.trade.toLowerCase()}`,
      body: `Your chart of accounts opens with ${pack.trade.toLowerCase()} revenue and expense lines, not a generic template you have to bend into shape.`,
    },
    {
      title: "Tax time, handled",
      body: `1099 tracking for your contractors, sales tax by jurisdiction, and a one-click CPA package for your ${customer}s' favorite season.`,
    },
  ];

  return (
    <div className="min-h-screen bg-[#0B0D10] text-gray-100">
      <div className="max-w-6xl mx-auto px-6 py-12">
        <div className="flex items-center justify-between mb-14">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg brand-mark" />
            <span className="font-display font-bold text-xl">Mind at Ease Solutions</span>
          </div>
          <div className="flex items-center gap-4">
            <MarketSelect value={marketCode} onChange={pickMarket} testId="trade-market-select" />
            <LanguageSwitcher compact />
            <Link to="/login" className="text-sm text-amber-500 hover:text-amber-400">
              Sign in <ArrowRight size={14} className="inline ms-1 rtl:-scale-x-100" />
            </Link>
          </div>
        </div>

        {/* Hero — the pack's own face, composed with the market's when chosen */}
        <div className="max-w-3xl mb-16">
          <div
            className="w-14 h-14 rounded-xl flex items-center justify-center mb-6"
            style={{ backgroundColor: `${accent}22`, color: accent }}
          >
            <FaceIcon name={pack.icon} size={30} />
          </div>
          <div className="text-xs uppercase tracking-widest text-gray-500 mb-3">{pack.trade}</div>
          <h1 className="font-display text-5xl font-bold leading-tight mb-4">{pack.product_name}</h1>
          <p className="text-xl text-gray-300 leading-relaxed mb-4">{pack.tagline}</p>
          {market && (
            <p data-testid="trade-market-face" className="text-sm text-gray-400 mb-8">
              <span className="text-base me-1">{market.flag}</span>
              {market.name} edition — {(skin.languages || []).join(" / ").toUpperCase()} · {skin.currency}
            </p>
          )}
          <div className="flex gap-3">
            <Link
              to="/login"
              className="px-5 py-3 rounded-lg bg-amber-600 hover:bg-amber-500 text-black font-semibold text-sm"
              style={market ? { backgroundColor: accent } : undefined}
            >
              Start with {pack.product_name} <ArrowRight size={15} className="inline ms-1 rtl:-scale-x-100" />
            </Link>
            <Link
              to="/industries"
              className="px-5 py-3 rounded-lg border border-[#222838] hover:border-amber-500/40 text-sm text-gray-300"
            >
              All industries
            </Link>
          </div>
        </div>

        {/* Services from the pack */}
        {(pack.services || []).length > 0 && (
          <div className="mb-16">
            <div className="text-xs uppercase tracking-widest text-gray-500 mb-4">
              Pre-loaded for your first week
            </div>
            <div className="flex flex-wrap gap-2">
              {pack.services.map((s) => (
                <span key={s} className="px-3 py-1.5 rounded-full bg-[#14171E] border border-[#222838] text-sm text-gray-300">
                  {s}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Features, worded with the pack's terminology */}
        <div className="grid sm:grid-cols-2 gap-4 mb-16">
          {features.map((f) => (
            <div key={f.title} className="rounded-xl bg-[#14171E] border border-[#222838] p-6">
              <div className="font-display font-semibold text-lg mb-2">{f.title}</div>
              <p className="text-sm text-gray-400 leading-relaxed">{f.body}</p>
            </div>
          ))}
        </div>

        {/* Setup checklist from the pack */}
        {(pack.checklist || []).length > 0 && (
          <div className="mb-16">
            <div className="text-xs uppercase tracking-widest text-gray-500 mb-4">Getting started</div>
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {pack.checklist.map((c) => (
                <div key={c.id} className="flex gap-3 rounded-xl bg-[#14171E] border border-[#222838] p-4">
                  <CheckCircle2 size={18} style={{ color: accent }} className="shrink-0 mt-0.5" />
                  <div>
                    <div className="text-sm font-medium text-gray-100">{c.label}</div>
                    <div className="text-xs text-gray-500 leading-relaxed">{c.hint}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="text-xs text-gray-600">
          One of 33 trade editions of Mind at Ease Solutions — same double-entry
          engine, your trade's face.{" "}
          <Link to="/industries" className="text-amber-500 hover:text-amber-400">
            Browse all industries
          </Link>
        </div>
      </div>
    </div>
  );
}
