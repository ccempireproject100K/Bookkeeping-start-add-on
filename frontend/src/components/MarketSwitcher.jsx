import { useEffect, useState } from "react";
import { Card } from "@/components/Page";
import { api, errMsg } from "@/lib/api";
import { asMarketList } from "@/lib/markets";
import { toast } from "sonner";
import { useNiche } from "@/context/NicheContext";
import { useAuth } from "@/context/AuthContext";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Globe } from "lucide-react";

/** Owner-only: set or clear the workspace's market face (accent, flag,
 *  languages, currency) — the market half of the edition skin, mirroring
 *  NicheSwitcher. Display/skin only; the base currency seeded at signup
 *  is untouched. English copy (like the public pages); i18n keys are a
 *  follow-up, same as other newer cards. */
export default function MarketSwitcher() {
  const { user } = useAuth();
  const { market, refresh } = useNiche();
  const [markets, setMarkets] = useState([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (user?.role === "owner") {
      api.get("/market-packs").then((d) => setMarkets(asMarketList(d) || [])).catch(() => setMarkets([]));
    }
  }, [user?.role]);

  if (user?.role !== "owner") return null;

  const current = market?.code || "none";
  const switchMarket = async (value) => {
    const code = value === "none" ? null : value;
    if ((code || "none") === current) return;
    setBusy(true);
    try {
      const r = await api.patch("/edition/market", { market_code: code });
      await refresh();
      toast.success(r.market ? `Now showing the ${r.market.flag} ${r.market.name} market face` : "Market face cleared");
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <Globe className="w-4 h-4" /> Market face
      </h3>
      <p className="text-sm text-gray-500 mb-4">
        Swap the workspace's market skin — accent color, flag, languages and currency
        compose over your trade face.
      </p>
      <Select value={current} onValueChange={switchMarket} disabled={busy || !markets.length}>
        <SelectTrigger className="bg-[#14171E] border-[#222838] max-w-sm" data-testid="market-select">
          <SelectValue placeholder="Choose a market" />
        </SelectTrigger>
        <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
          <SelectItem value="none" data-testid="market-option-none">
            No market — trade face only
          </SelectItem>
          {markets.map((m) => (
            <SelectItem key={m.code} value={m.code} data-testid={`market-option-${m.code.toLowerCase()}`}>
              {m.flag} {m.name} — {m.currency}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Card>
  );
}
