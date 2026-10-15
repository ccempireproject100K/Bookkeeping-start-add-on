import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { Tag, TrendingUp } from "lucide-react";

export default function Pricing() {
  const [loaded, setLoaded] = useState(false);
  const [hist, setHist] = useState(null);
  const [res, setRes] = useState(null);
  const [form, setForm] = useState({ description: "", estimated_cost_cents: "", target_margin_pct: "40" });

  useEffect(() => { api.get("/pricing/history").then(setHist).catch(() => {}); setLoaded(true); }, []);

  const guide = async () => {
    try {
      setRes(await api.post("/pricing/guide", {
        description: form.description,
        estimated_cost_cents: Math.round(parseFloat(form.estimated_cost_cents || 0) * 100),
        target_margin_pct: parseFloat(form.target_margin_pct) || 40,
      }));
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!loaded) return <Loading />;
  return (
    <div className="space-y-6">
      <PageHeader title="Price Guidance" subtitle="What should you quote? Your own win history knows." />
      <div className="grid lg:grid-cols-[380px_1fr] gap-6">
        <Card className="p-5 space-y-3 h-fit">
          <div className="text-sm font-medium flex items-center gap-2"><Tag size={15} /> New quote</div>
          <div><Label className="text-xs">Job description</Label>
            <Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="AC replacement, 3-ton" className="bg-[#0F1218] border-[#222838]" /></div>
          <div><Label className="text-xs">Estimated cost $</Label>
            <Input type="number" value={form.estimated_cost_cents} onChange={(e) => setForm({ ...form, estimated_cost_cents: e.target.value })}
              className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <div><Label className="text-xs">Target margin %</Label>
            <Input type="number" value={form.target_margin_pct} onChange={(e) => setForm({ ...form, target_margin_pct: e.target.value })}
              className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <Button className="w-full" onClick={guide} data-testid="pricing-guide">Get guidance</Button>
          {hist && hist.samples > 0 && (
            <p className="text-xs text-gray-500">Based on {hist.samples} decided quotes · {hist.win_rate_overall}% overall win rate · median won {money(hist.median_won_cents)}</p>
          )}
        </Card>
        <div className="space-y-4">
          {res && <>
            <Card className="p-6 bg-emerald-500/5">
              <div className="text-xs text-gray-500">Suggested price</div>
              <div className="text-4xl font-semibold text-emerald-400">{money(res.suggested_cents)}</div>
              <div className="text-sm text-gray-400 mt-1">Healthy range: {money(res.range_low_cents)} – {money(res.range_high_cents)}</div>
              <div className="flex items-center gap-2 mt-2 text-sm">
                <TrendingUp size={14} className="text-blue-400" />
                <span>~{res.win_rate_at_price}% win rate at this level</span>
                <span className={`text-xs px-2 py-0.5 rounded-full ${res.confidence === "high" ? "bg-emerald-500/15 text-emerald-400" : res.confidence === "medium" ? "bg-amber-500/15 text-amber-400" : "bg-gray-500/15 text-gray-400"}`}>{res.confidence} confidence</span>
              </div>
            </Card>
            <Card className="p-5">
              <div className="text-sm font-medium mb-2">Why this price</div>
              <ul className="text-sm text-gray-400 space-y-1 list-disc list-inside">
                {res.notes.map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            </Card>
          </>}
          {!res && <Card className="p-10 text-center text-gray-500 text-sm">Enter the job details and get a data-backed quote.</Card>}
          {!!hist?.bands?.length && (
            <Card className="p-5">
              <div className="text-sm font-medium mb-2">Your win rate by price band</div>
              {hist.bands.map((b, i) => (
                <div key={i} className="flex items-center gap-3 py-1.5 text-sm">
                  <span className="w-40 font-mono text-xs">{money(b.low_cents)} – {money(b.high_cents)}</span>
                  <div className="flex-1 h-2 bg-[#1a2030] rounded-full overflow-hidden">
                    <div className="h-full bg-blue-500 rounded-full" style={{ width: `${b.win_rate}%` }} /></div>
                  <span className="w-24 text-right text-xs text-gray-400">{b.win_rate}% · {b.samples} quotes</span>
                </div>
              ))}
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
