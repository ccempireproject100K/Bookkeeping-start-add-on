import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Bot, Loader2, Send, Sparkles, Trash2 } from "lucide-react";
import { apiDelete, apiGet, apiStream } from "@/lib/api";
import { errMsg } from "@/lib/format";
import type { AiMessage } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

const SUGGESTIONS = ["What sold best this month?", "What should I reorder this week?", "How much commission does each barber get?", "Which lots expire soon?"];

/** "Ask the shop": streaming ChatGPT answers grounded in this workspace's own numbers. */
export default function Assistant() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [pending, setPending] = useState<{ q: string; a: string } | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const { data: msgs = [] } = useQuery({ queryKey: ["ai-chat"], queryFn: () => apiGet<AiMessage[]>("/ai/chat"), enabled: open });
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs.length, pending?.a]);

  const clear = useMutation({ mutationFn: () => apiDelete("/ai/chat"), onSuccess: () => void qc.invalidateQueries({ queryKey: ["ai-chat"] }) });

  const ask = async (q: string) => {
    if (!q.trim() || pending) return;
    setText("");
    setPending({ q, a: "" });
    try {
      await apiStream("/ai/chat/stream", { message: q }, (e) => {
        if (typeof e.delta === "string") setPending((p) => (p ? { ...p, a: p.a + (e.delta as string) } : p));
        if (typeof e.error === "string") toast.error(e.error);
      });
    } catch (e) {
      toast.error(errMsg(e));
    }
    await qc.invalidateQueries({ queryKey: ["ai-chat"] });
    setPending(null);
  };

  const bubble = (role: string, content: string, key: string, live = false) => (
    <div key={key} className={cn("flex", role === "user" ? "justify-end" : "justify-start")} data-testid={`ai-msg-${role}`}>
      <div className={cn("max-w-[85%] whitespace-pre-wrap rounded-2xl px-3.5 py-2 text-sm animate-rise",
        role === "user" ? "rounded-br-sm bg-primary text-primary-foreground" : "rounded-bl-sm bg-muted")}>
        {content || (live ? <Loader2 className="size-4 animate-spin" /> : "")}
      </div>
    </div>
  );

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)} data-testid="ai-open-button"><Sparkles /> Ask</Button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-md" data-testid="ai-assistant-sheet">
          <div className="flex items-center gap-3 border-b border-border p-4 pr-12">
            <div className="grid size-9 place-items-center rounded-full bg-primary/10 text-primary"><Bot className="size-4" /></div>
            <div className="flex-1">
              <SheetTitle>Ask the shop</SheetTitle>
              <SheetDescription className="text-xs">Answers from your live numbers · GPT-5 mini</SheetDescription>
            </div>
            {msgs.length > 0 && <Button variant="ghost" size="icon-sm" onClick={() => clear.mutate()} data-testid="ai-clear-button" aria-label="Clear chat"><Trash2 /></Button>}
          </div>
          <div className="flex-1 space-y-3 overflow-y-auto p-4" data-testid="ai-messages">
            {msgs.length === 0 && !pending && (
              <div className="space-y-2">
                <p className="text-sm text-muted-foreground">Try one of these:</p>
                {SUGGESTIONS.map((s, i) => (
                  <button key={s} onClick={() => void ask(s)} data-testid={`ai-suggestion-${i}`}
                    className="block w-full rounded-lg border border-border px-3 py-2 text-left text-sm transition-colors duration-150 hover:border-primary hover:bg-primary/5">{s}</button>
                ))}
              </div>
            )}
            {msgs.map((m) => bubble(m.role, m.content, m.id))}
            {pending && <>{bubble("user", pending.q, "pq")}{bubble("assistant", pending.a, "pa", true)}</>}
            <div ref={end} />
          </div>
          <form className="flex gap-2 border-t border-border p-3" onSubmit={(e) => { e.preventDefault(); void ask(text); }}>
            <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask about sales, stock, commission…" disabled={!!pending} data-testid="ai-input" />
            <Button type="submit" size="icon" disabled={!text.trim() || !!pending} data-testid="ai-send-button" aria-label="Send"><Send /></Button>
          </form>
          <p className="px-3 pb-2 text-[10px] text-muted-foreground">AI can make mistakes — double-check numbers in Reports before acting.</p>
        </SheetContent>
      </Sheet>
    </>
  );
}
