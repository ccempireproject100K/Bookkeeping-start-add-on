import { useEffect, useState } from "react";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api";

// Multi-currency Phase 4: currency dropdown fed by GET /api/currencies.
// Falls back to USD-only when the backend list is unreachable.
let _cache = null;

export function useCurrencies() {
  const [list, setList] = useState(_cache);
  useEffect(() => {
    if (_cache) return;
    api.get("/currencies").then((rows) => {
      _cache = Array.isArray(rows) ? rows : [];
      setList(_cache);
    }).catch(() => setList([]));
  }, []);
  return list || [{ code: "USD", name: "US Dollar", symbol: "$", decimals: 2, stripe: true }];
}

export default function CurrencyPicker({ value, onChange, testid, label = "Currency" }) {
  const currencies = useCurrencies();
  return (
    <div>
      <Label className="text-xs">{label}</Label>
      <Select value={value || "USD"} onValueChange={onChange}>
        <SelectTrigger data-testid={testid || "currency-picker"} className="bg-[#0F1218] border-[#222838]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">
          {currencies.map((c) => (
            <SelectItem key={c.code} value={c.code}>
              {c.code} · {c.name} ({c.symbol})
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
