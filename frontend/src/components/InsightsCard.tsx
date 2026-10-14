import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw, Sparkles } from "lucide-react";
import { apiGet } from "@/lib/api";
import { errMsg, fmtDateTime } from "@/lib/format";
import type { AiInsights } from "@/lib/types";
import { Panel } from "@/components/Common";
import { Button } from "@/components/ui/button";

/** Daily AI briefing (cached server-side per day; refresh regenerates). */
export default function InsightsCard() {
  const qc = useQueryClient();
  const { data, isFetching, error } = useQuery({ queryKey: ["ai-insights"], queryFn: () => apiGet<AiInsights>("/ai/insights"), staleTime: 30 * 60_000, retry: false });
  const refresh = async () => {
    await qc.fetchQuery({ queryKey: ["ai-insights"], queryFn: () => apiGet<AiInsights>("/ai/insights?refresh=true") });
  };
  const lines = (data?.text ?? "").split("\n").map((l) => l.replace(/^[-•*]\s*/, "").trim()).filter(Boolean);

  return (
    <Panel className="relative mt-3 overflow-hidden p-4 md:p-5" data-testid="ai-insights-card">
      <div className="pointer-events-none absolute -right-16 -top-16 size-48 rounded-full bg-primary/10 blur-3xl" />
      <div className="flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-base font-semibold"><Sparkles className="size-4 text-primary" /> Today's briefing</h3>
        <Button variant="ghost" size="xs" onClick={() => void refresh()} disabled={isFetching} data-testid="ai-insights-refresh">
          {isFetching ? <Loader2 className="animate-spin" /> : <RefreshCw />} Refresh
        </Button>
      </div>
      {isFetching && !data && <p className="mt-3 text-sm text-muted-foreground" data-testid="ai-insights-loading">Reading your numbers…</p>}
      {error && !data && <p className="mt-3 text-sm text-red-500" data-testid="ai-insights-error">{errMsg(error)}</p>}
      {data && (
        <ul className="mt-3 space-y-2" data-testid="ai-insights-list">
          {lines.map((l, i) => (
            <li key={i} className="flex gap-2.5 text-sm animate-rise" style={{ animationDelay: `${i * 60}ms` }}>
              <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-primary" />{l}
            </li>
          ))}
        </ul>
      )}
      {data && <div className="mt-3 text-[10px] text-muted-foreground">AI-generated from your data · {data.model} · {fmtDateTime(data.created_at)} · check before acting</div>}
    </Panel>
  );
}
