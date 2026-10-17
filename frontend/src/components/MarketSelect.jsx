import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { asMarketList } from "@/lib/markets";

/** Market picker fed by GET /market-packs (all 46 market faces).
 *  A plain styled <select> — the public pages are dependency-light.
 *  The empty value means "no market": the trade-only face. */
export default function MarketSelect({ value, onChange, testId = "market-select", className = "" }) {
  const [markets, setMarkets] = useState([]);

  useEffect(() => {
    let live = true;
    api.get("/market-packs")
      .then((data) => { if (live) setMarkets(asMarketList(data) || []); })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  return (
    <select
      data-testid={testId}
      value={value || ""}
      onChange={(e) => onChange(e.target.value || "")}
      className={`bg-[#14171E] border border-[#222838] rounded-lg text-sm text-gray-200 px-3 py-2 ${className}`}
      aria-label="Market"
    >
      <option value="">🌍 All markets</option>
      {markets.map((m) => (
        <option key={m.code} value={m.code}>
          {m.flag} {m.name}
        </option>
      ))}
    </select>
  );
}
