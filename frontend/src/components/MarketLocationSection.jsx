import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { asMarketList, findMarket } from "@/lib/markets";
import MarketSelect from "@/components/MarketSelect";

/** "Where are you located?" — the market-face section for the public
 *  pages (owner ruling 2026-11-01). States which market edition the
 *  visitor is seeing and WHY — their saved choice, a detected
 *  location, the US default, or their explicit no-market pick — and
 *  lets them change it in one tap. The pick is stored by the parent
 *  page's handler, so it wins on every later visit; detection never
 *  locks. Copy is plain English like the rest of the public pages
 *  (the language switcher localizes the page chrome separately). */
export default function MarketLocationSection({ code, source, onPick }) {
  const [markets, setMarkets] = useState([]);

  useEffect(() => {
    let live = true;
    api.get("/market-packs")
      .then((data) => { if (live) setMarkets(asMarketList(data) || []); })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  const market = findMarket(markets, code);
  const name = market?.name || code || "";
  const flag = market?.flag || "";

  return (
    <section
      data-testid="market-location-section"
      className="rounded-xl bg-[#14171E] border border-[#222838] p-6 mt-16"
    >
      <h2 className="font-display font-semibold text-lg text-gray-100 mb-2">
        Where are you located?
      </h2>
      <p className="text-sm text-gray-400 leading-relaxed max-w-2xl">
        We show the edition for your market — languages, details and
        pricing for where you are.
      </p>
      <p data-testid="market-location-note" className="text-sm text-gray-300 mt-3">
        {source === "choice" && (
          <>{flag} You're seeing the {name} edition — your saved choice. You can change it any time.</>
        )}
        {source === "detected" && (
          <>{flag} We detected your location as {name}, so you're seeing that edition. Wrong? Pick yours below.</>
        )}
        {source === "default" && (
          <>🇺🇸 You're seeing the United States edition — our default when we can't detect a location. Pick yours below if that's not you.</>
        )}
        {source === "none" && (
          <>You're seeing the trade-only edition, with no market selected — your saved choice. Pick a market below any time.</>
        )}
      </p>
      <div className="mt-4">
        <MarketSelect value={code} onChange={onPick} testId="market-location-select" />
      </div>
    </section>
  );
}
