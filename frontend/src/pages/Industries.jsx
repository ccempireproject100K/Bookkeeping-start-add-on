import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { asPackList } from "@/lib/packs";
import { asMarketList, findMarket, readMarketChoice, writeMarketChoice } from "@/lib/markets";
import FaceIcon from "@/components/FaceIcon";
import MarketSelect from "@/components/MarketSelect";
import LanguageSwitcher from "@/components/LanguageSwitcher";

/** Public index of all trade editions. Data-driven from the niche packs
 *  (GET /niches) — a new pack file on the backend is a new card here.
 *  Picking a market (GET /market-packs) re-skins the page with the
 *  composed trade x market face: the market's accent wins, exactly as
 *  the backend resolver composes it. The choice persists (localStorage)
 *  and is shared with the trade pages and login. */
export default function Industries() {
  const [packs, setPacks] = useState(null);
  const [err, setErr] = useState("");
  const [marketCode, setMarketCode] = useState(() => readMarketChoice());
  const [market, setMarket] = useState(null);

  useEffect(() => {
    document.title = "Industries — Mind at Ease Solutions";
    api.get("/niches")
      .then((data) => {
        // Only a real array of packs may reach the grid — anything else
        // (e.g. an HTML page from a misconfigured API base) is an error,
        // not renderable data.
        const list = asPackList(data);
        if (list) setPacks(list);
        else setErr("Could not load the industry list.");
      })
      .catch(() => setErr("Could not load the industry list."));
  }, []);

  useEffect(() => {
    if (!marketCode) { setMarket(null); return; }
    let live = true;
    api.get("/market-packs")
      .then((data) => { if (live) setMarket(findMarket(data, marketCode)); })
      .catch(() => {});
    return () => { live = false; };
  }, [marketCode]);

  const pickMarket = (code) => {
    writeMarketChoice(code);
    setMarketCode(code);
  };

  const accent = market?.colors?.accent || "#f59e0b";

  return (
    <div className="min-h-screen bg-[#0B0D10] text-gray-100">
      <div className="max-w-6xl mx-auto px-6 py-12">
        <div className="flex items-center justify-between mb-12">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg brand-mark" />
            <span className="font-display font-bold text-xl">Mind at Ease Solutions</span>
          </div>
          <div className="flex items-center gap-4">
            <LanguageSwitcher compact />
            <Link to="/login" className="text-sm hover:opacity-80" style={{ color: accent }}>
              Sign in <ArrowRight size={14} className="inline ml-1" />
            </Link>
          </div>
        </div>

        <h1 className="font-display text-4xl font-bold leading-tight mb-4">
          Bookkeeping that speaks <span style={{ color: accent }}>your trade</span>.
        </h1>
        <p className="text-gray-400 leading-relaxed max-w-2xl mb-6">
          One double-entry platform, {packs ? packs.length : ""} trade editions.
          Pick your industry — the branding, terminology, chart of accounts and
          setup checklist all shape themselves around how your business talks.
        </p>

        <div className="flex flex-wrap items-center gap-3 mb-12">
          <MarketSelect value={marketCode} onChange={pickMarket} testId="industries-market-select" />
          {market && (
            <span data-testid="industries-market-face" className="text-sm text-gray-300">
              {market.flag} {market.name} edition —{" "}
              {(market.languages || []).join(" / ").toUpperCase()} · {market.currency}
            </span>
          )}
        </div>

        {err && <p className="text-rose-400 text-sm">{err}</p>}
        {!packs && !err && <p className="text-gray-500 text-sm">Loading industries…</p>}

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {(packs || []).map((p) => (
            <Link
              key={p.id}
              to={`/industries/${p.id}`}
              data-testid={`industry-card-${p.id}`}
              className="group rounded-xl bg-[#14171E] border border-[#222838] hover:border-amber-500/40 transition-colors p-5"
            >
              <div
                className="w-11 h-11 rounded-lg flex items-center justify-center mb-4"
                style={{
                  // Composed face: a chosen market's accent wins over the
                  // trade's; with no market, each card keeps its trade accent.
                  backgroundColor: `${market ? accent : p.colors?.accent || "#f59e0b"}22`,
                  color: market ? accent : p.colors?.accent || "#f59e0b",
                }}
              >
                <FaceIcon name={p.icon} size={22} />
              </div>
              <div className="font-display font-semibold text-lg text-gray-100">{p.trade}</div>
              <div className="text-xs text-gray-500 mb-2">{p.product_name}</div>
              <p className="text-sm text-gray-400 leading-relaxed">{p.tagline}</p>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
