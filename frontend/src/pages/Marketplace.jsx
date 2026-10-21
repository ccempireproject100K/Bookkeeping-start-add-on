import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Store, Search, CheckCircle2, KeyRound, Clock3, ListChecks } from "lucide-react";

// Honest status language only: a listing is "live" only when the
// backend's key-gating says the integration actually works today.
const STATUS_TONE = {
  live: "bg-emerald-500/15 text-emerald-400",
  needs_key: "bg-amber-500/15 text-amber-400",
  coming_soon: "bg-gray-500/15 text-gray-400",
};
const STATUS_ICON = {
  live: CheckCircle2,
  needs_key: KeyRound,
  coming_soon: Clock3,
};
const STATUS_BLURB = {
  live: "Connected and working in this workspace today.",
  needs_key: "Built and ready — it switches on as soon as the operator adds the provider key in the server settings.",
  coming_soon: "On the roadmap. No integration code exists for this one yet, so there is nothing to connect today.",
};

function StatusBadge({ status, label }) {
  const Icon = STATUS_ICON[status] || Clock3;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_TONE[status] || STATUS_TONE.coming_soon}`}>
      <Icon className="h-3 w-3" /> {label || status}
    </span>
  );
}

export default function Marketplace() {
  const [data, setData] = useState(null);
  const [category, setCategory] = useState("All");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    api.get("/marketplace").then(setData).catch((e) => {
      toast.error(errMsg(e));
      setData({ categories: [], listings: [] });
    });
  }, []);

  const listings = useMemo(() => {
    const all = data?.listings || [];
    const needle = q.trim().toLowerCase();
    return all.filter((l) =>
      (category === "All" || l.category === category) &&
      (!needle || `${l.name} ${l.provider} ${l.tagline}`.toLowerCase().includes(needle)));
  }, [data, category, q]);

  if (!data) return <Loading />;

  return (
    <div>
      <PageHeader title="App Marketplace" subtitle="Integrations that plug into your books. A listing only shows Live when it truly works today — anything waiting on a provider key says so." />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="absolute left-2 top-2.5 h-4 w-4 text-gray-400" />
          <Input className="pl-8 w-64" placeholder="Search integrations…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        {["All", ...(data.categories || []).map((c) => c.name)].map((c) => (
          <Button key={c} size="sm" variant={category === c ? "default" : "outline"} onClick={() => setCategory(c)}>
            {c}{(data.categories || []).find((x) => x.name === c) ? ` (${(data.categories || []).find((x) => x.name === c).count})` : ""}
          </Button>
        ))}
      </div>

      {listings.length === 0 && (
        <Card><p className="text-sm text-gray-400">No integrations match. Categories with nothing in them yet are on the roadmap — we only list what exists or is honestly planned.</p></Card>
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {listings.map((l) => (
          <button key={l.slug} onClick={() => setSelected(l)} className="text-left">
            <Card>
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2 font-semibold"><Store className="h-4 w-4" /> {l.name}</div>
                  <div className="text-xs text-gray-400">{l.provider} · {l.category}</div>
                </div>
                <StatusBadge status={l.status} label={l.status_label} />
              </div>
              <p className="mt-2 text-sm text-gray-300">{l.tagline}</p>
              <p className="mt-2 text-xs text-gray-500">{l.cost_text}</p>
            </Card>
          </button>
        ))}
      </div>

      <Dialog open={!!selected} onOpenChange={(o) => !o && setSelected(null)}>
        <DialogContent>
          {selected && (
            <>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2">{selected.name} <StatusBadge status={selected.status} label={selected.status_label} /></DialogTitle>
              </DialogHeader>
              <div className="space-y-3 text-sm">
                <p className="text-gray-300">{selected.description}</p>
                <p className="text-gray-400">{STATUS_BLURB[selected.status]}</p>
                <div>
                  <div className="mb-1 flex items-center gap-1 font-medium"><ListChecks className="h-4 w-4" /> What you need to connect</div>
                  <ul className="list-disc pl-5 text-gray-300">
                    {(selected.connect_needs || []).map((n, i) => <li key={i}>{n}</li>)}
                  </ul>
                </div>
                <p className="text-gray-400"><span className="font-medium text-gray-200">Cost:</span> {selected.cost_text}</p>
                {selected.setup_doc && <p className="text-xs text-gray-500">Operator setup guide: {selected.setup_doc} (repo root).</p>}
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
