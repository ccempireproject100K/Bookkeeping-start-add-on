import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { HelpCircle, Check, Brain, Trash2 } from "lucide-react";

/**
 * Categorization review queue (gap #7): the transactions auto-categorization
 * was genuinely unsure about, each with its best guess and a plain-words
 * reason. Confirm the guess or fix it. A fix asks — once — whether to
 * remember the pattern; learning can be switched off entirely, and then
 * nothing is remembered, ever.
 */
export default function CatReviewPanel({ coa, canManage, onResolved }) {
  const [items, setItems] = useState(null);
  const [stats, setStats] = useState(null);
  const [learning, setLearning] = useState(null);
  const [patterns, setPatterns] = useState([]);
  const [picks, setPicks] = useState({});       // itemId -> chosen code
  const [ask, setAsk] = useState(null);          // itemId awaiting remember answer

  const catOptions = (coa || []).filter((c) => !c.archived);

  const load = () => {
    api.get("/cat-review/queue").then(setItems).catch(() => setItems([]));
    api.get("/cat-review/stats").then(setStats).catch(() => setStats(null));
    api.get("/cat-review/settings").then((s) => setLearning(s.learning_enabled)).catch(() => setLearning(true));
    api.get("/cat-review/patterns").then(setPatterns).catch(() => setPatterns([]));
  };
  useEffect(load, []);

  const resolve = async (item, code, remember) => {
    try {
      const r = await api.post(`/cat-review/queue/${item.id}/resolve`, {
        category_code: code, remember: !!remember,
      });
      toast.success(r.learned
        ? "Posted — and I'll remember that pattern"
        : "Posted to ledger");
      setAsk(null);
      load();
      if (onResolved) onResolved();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const post = (item) => {
    const code = picks[item.id] || item.guess_code || "";
    if (!code) { toast.error("Pick a category first"); return; }
    const isCorrection = code !== item.guess_code;
    // Learning is asked about only after a correction, and only when the
    // tenant hasn't switched learning off.
    if (isCorrection && learning) { setAsk(item.id); return; }
    resolve(item, code, false);
  };

  const toggleLearning = async (v) => {
    try {
      const r = await api.patch("/cat-review/settings", { learning_enabled: v });
      setLearning(r.learning_enabled);
      toast.success(v ? "Learning on — I'll ask before remembering a pattern"
                      : "Learning off — nothing will be remembered");
    } catch (e) { toast.error(errMsg(e)); }
  };

  const forget = async (p) => {
    try { await api.del(`/cat-review/patterns/${p.id}`); toast.success("Pattern forgotten"); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  return (
    <div data-testid="cat-review-panel">
      <Card className="mb-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex-1 min-w-56">
            <div className="text-xs uppercase tracking-wider text-gray-500 flex items-center gap-1">
              <HelpCircle size={13} /> Needs your eye
            </div>
            <div className="text-sm text-gray-300 mt-1" data-testid="cat-review-stats">
              {stats
                ? <>{stats.pending} waiting · {stats.flagged_this_week} flagged this week · {stats.learned_count} pattern{stats.learned_count === 1 ? "" : "s"} remembered</>
                : "Loading…"}
            </div>
            <div className="text-xs text-gray-500 mt-1">
              Only the transactions categorization is genuinely unsure about land here —
              as patterns are remembered, this list gets shorter.
            </div>
          </div>
          {canManage && learning !== null && (
            <div className="flex items-center gap-2" data-testid="cat-learning-setting">
              <Switch data-testid="cat-learning-toggle" checked={learning} onCheckedChange={toggleLearning} />
              <div>
                <div className="text-sm text-gray-200 flex items-center gap-1"><Brain size={14} /> Learn from my fixes</div>
                <div className="text-xs text-gray-500">{learning
                  ? "On — I'll ask before remembering each new pattern."
                  : "Off — nothing is remembered; every fix is one-time."}</div>
              </div>
            </div>
          )}
        </div>
      </Card>

      {items === null && <div className="text-gray-500 text-sm py-8 text-center">Loading…</div>}
      {items && items.length === 0 && (
        <Card><div className="text-gray-500 text-sm py-8 text-center" data-testid="cat-review-empty">
          Nothing needs your eye. Every transaction so far matched a rule — or a remembered pattern — with confidence.
        </div></Card>
      )}
      <div className="space-y-3">
        {(items || []).map((item) => {
          const pick = picks[item.id] ?? item.guess_code ?? "";
          return (
            <Card key={item.id} data-testid={`cat-review-item-${item.id}`}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-56 flex-1">
                  <div className="text-gray-100 font-medium">{item.description}</div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    {fmtDate(item.txn_date)} · <span className={`font-mono ${item.amount_cents < 0 ? "text-rose-400" : "text-emerald-400"}`}>{money(item.amount_cents)}</span>
                  </div>
                  <div className="text-sm text-amber-400/90 mt-2" data-testid={`cat-review-reason-${item.id}`}>{item.reason}</div>
                  <div className="text-xs text-gray-400 mt-1">
                    {item.guess_code
                      ? <>Best guess: <span className="text-gray-200">{item.guess_code} · {item.guess_name || ""}</span></>
                      : "No guess yet — pick where this belongs."}
                  </div>
                </div>
                {canManage && (
                  <div className="flex items-end gap-2">
                    <div className="w-56">
                      <Label className="text-xs">Category</Label>
                      <Select value={pick} onValueChange={(v) => setPicks({ ...picks, [item.id]: v })}>
                        <SelectTrigger data-testid={`cat-review-pick-${item.id}`} className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder="Choose account" /></SelectTrigger>
                        <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">{catOptions.map((c) => <SelectItem key={c.code} value={c.code}>{c.code} · {c.name}</SelectItem>)}</SelectContent>
                      </Select>
                    </div>
                    <Button data-testid={`cat-review-post-${item.id}`} disabled={!pick} onClick={() => post(item)} className="bg-amber-600 hover:bg-amber-500 text-black">
                      <Check size={14} className="mr-1" />Post it
                    </Button>
                  </div>
                )}
              </div>
              {ask === item.id && (
                <div className="mt-3 rounded-lg bg-[#0F1218] border border-amber-500/30 px-4 py-3 flex flex-wrap items-center gap-3" data-testid={`cat-review-remember-${item.id}`}>
                  <div className="text-sm text-gray-200 flex-1 min-w-56">
                    Want me to remember this pattern for next time? Transactions like this one
                    will be filed the same way — and won't land here again.
                  </div>
                  <Button size="sm" data-testid={`cat-review-remember-yes-${item.id}`}
                    onClick={() => resolve(item, pick, true)} className="bg-amber-600 hover:bg-amber-500 text-black">
                    Remember it
                  </Button>
                  <Button size="sm" variant="outline" data-testid={`cat-review-remember-no-${item.id}`}
                    onClick={() => resolve(item, pick, false)} className="border-[#222838]">
                    Just this once
                  </Button>
                </div>
              )}
            </Card>
          );
        })}
      </div>

      {patterns.length > 0 && (
        <Card className="mt-4">
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-2">Remembered patterns</div>
          <div className="space-y-2" data-testid="cat-patterns-list">
            {patterns.map((p) => (
              <div key={p.id} className="flex items-center justify-between bg-[#0F1218] border border-[#222838] rounded-lg px-4 py-2 text-sm">
                <span className="text-gray-300">“{p.sample_description}” → <span className="text-amber-400">{p.category_code} {p.category_name}</span>
                  <span className="text-gray-600 text-xs ml-2">learned from {p.times_corrected} fix{p.times_corrected === 1 ? "" : "es"}</span></span>
                {canManage && <button onClick={() => forget(p)} data-testid={`cat-pattern-forget-${p.id}`} className="text-gray-600 hover:text-rose-400 flex items-center gap-1 text-xs"><Trash2 size={13} /> Forget</button>}
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
