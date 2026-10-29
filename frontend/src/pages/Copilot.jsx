import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { dollars, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import { Sparkles, Send, Wand2, Check, X, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import PlanGate from "@/components/PlanGate";
const STATUS_CLS = {
  pending: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  approved: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  rejected: "bg-gray-500/10 text-gray-400 border-gray-500/20",
  executed: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
};
export default function Copilot() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [snap, setSnap] = useState(null);
  const [msgs, setMsgs] = useState([]);
  const [text, setText] = useState("");
  const [asking, setAsking] = useState(false);
  const [actions, setActions] = useState(null);
  const [proposing, setProposing] = useState(false);
  const end = useRef(null);
  const loadSnap = () => api.get("/copilot/snapshot").then(setSnap).catch(() => setSnap({}));
  const loadActions = () => api.get("/copilot/actions").then(setActions).catch(() => setActions([]));
  useEffect(() => {
    loadSnap();
    loadActions();
  }, []);
  useEffect(() => {
    end.current?.scrollIntoView({
      behavior: "smooth"
    });
  }, [msgs.length]);
  const ask = async e => {
    e?.preventDefault();
    const prompt = text.trim();
    if (!prompt || asking) return;
    setText("");
    setMsgs(m => [...m, {
      role: "user",
      text: prompt
    }, {
      role: "pending",
      text: ""
    }]);
    setAsking(true);
    try {
      const r = await api.post("/copilot/chat", {
        prompt
      });
      setMsgs(m => [...m.slice(0, -1), {
        role: "assistant",
        text: r.reply
      }]);
    } catch (err) {
      setMsgs(m => [...m.slice(0, -1), {
        role: "error",
        text: errMsg(err)
      }]);
    }
    setAsking(false);
  };
  const propose = async () => {
    setProposing(true);
    try {
      const r = await api.post("/copilot/actions/propose", {});
      toast.success(r.length ? `Proposed ${r.length} action${r.length > 1 ? "s" : ""} for your approval` : "Nothing worth doing right now");
      loadActions();
    } catch (e) {
      toast.error(errMsg(e));
    }
    setProposing(false);
  };
  const decide = async (a, approve) => {
    if (approve && !confirm(`Run this action?\n\n${a.title}\n${a.detail}`)) return;
    try {
      await api.post(`/copilot/actions/${a.id}/${approve ? "approve" : "reject"}`, {});
      toast.success(approve ? "Action executed" : "Action dismissed");
      loadActions();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (snap === null || actions === null) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("copilot.aiCopilot")} subtitle="Grounded answers and approval-gated actions" testid="copilot-page">
        <span className="flex items-center gap-1 text-xs text-amber-400"><Sparkles size={14} /> {t("copilot.ai")}</span>
      </PageHeader>

      {/* Workspace snapshot */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6" data-testid="copilot-snapshot">
        {[["Accounts receivable", dollars(snap.accounts_receivable || 0)], ["Accounts payable", dollars(snap.accounts_payable || 0)], ["Overdue invoices", String((snap.overdue_invoices || []).length)], ["Low stock items", String((snap.low_stock || []).length)]].map(([label, value]) => <Card key={label} className="p-4">
            <div className="text-xs uppercase tracking-wider text-gray-500">{label}</div>
            <div className="text-2xl font-mono mt-1 text-gray-100">{value}</div>
          </Card>)}
      </div>

      <div className="grid lg:grid-cols-2 gap-6">
        {/* Chat */}
        <Card className="p-0 overflow-hidden flex flex-col" data-testid="copilot-chat">
          <div className="px-4 py-3 border-b border-[#222838] text-xs uppercase tracking-wider text-gray-500">{t("copilot.askAboutYourNumbers")}</div>
          <div className="flex-1 min-h-64 max-h-96 overflow-y-auto p-4 space-y-3">
            {msgs.length === 0 && <div className="text-sm text-gray-500">{t("copilot.tryHowMuchIsOverdueOr")}{!snap.overdue_invoices?.length && !snap.low_stock?.length && " Your books look quiet — nice work."}
              </div>}
            {msgs.map((m, i) => <div key={i} className={`text-sm rounded-lg px-3 py-2 whitespace-pre-wrap max-w-full ${m.role === "user" ? "bg-amber-600/10 border border-amber-500/20 text-gray-100 ms-8" : m.role === "error" ? "bg-rose-600/10 border border-rose-500/20 text-rose-300" : "bg-[#0F1218] border border-[#222838] text-gray-200 me-8"}`}>
                {m.role === "pending" ? <Loader2 size={14} className="animate-spin text-gray-500" /> : m.text}
              </div>)}
            <div ref={end} />
          </div>
          <form onSubmit={ask} className="flex gap-2 p-3 border-t border-[#222838]">
            <Input data-testid="copilot-input" value={text} onChange={e => setText(e.target.value)} placeholder={t("copilot.askTheCopilot")} className="bg-[#0F1218] border-[#222838]" disabled={asking} />
            <Button data-testid="copilot-send" type="submit" disabled={!text.trim() || asking} className="bg-amber-600 hover:bg-amber-500 text-black shrink-0"><Send size={15} /></Button>
          </form>
          <div className="px-4 pb-2 text-[10px] text-gray-600">{t("copilot.aiCanMakeMistakesDoubleCheck")}</div>
        </Card>

        {/* Actions — Medium plan and up */}
        <PlanGate feature="copilot_actions">
        <Card className="p-0 overflow-hidden" data-testid="copilot-actions">
          <div className="px-4 py-3 border-b border-[#222838] flex items-center justify-between gap-2">
            <div className="text-xs uppercase tracking-wider text-gray-500">{t("copilot.proposedActions")}</div>
            {canManage && <Button data-testid="copilot-propose" size="sm" variant="outline" disabled={proposing} onClick={propose} className="border-[#222838]">
                {proposing ? <Loader2 size={14} className="me-1 animate-spin" /> : <Wand2 size={14} className="me-1" />} {t("copilot.suggestActions")}</Button>}
          </div>
          <div className="p-4 space-y-3 max-h-[28rem] overflow-y-auto">
            <p className="text-xs text-gray-500">{t("copilot.theCopilotProposesActionsNothingRuns")}</p>
            {actions.length === 0 && <div className="text-sm text-gray-500 py-6 text-center">{t("copilot.noActionsYetTapSuggestActions")}</div>}
            {actions.map(a => <div key={a.id} className="rounded-lg bg-[#0F1218] border border-[#222838] p-3" data-testid={`copilot-action-${a.id}`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="text-sm font-medium text-gray-100">{a.title}</div>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium border capitalize ${STATUS_CLS[a.status] || ""}`}>{a.status}</span>
                </div>
                <p className="mt-1 text-xs text-gray-400">{a.detail}</p>
                {a.brief && (a.brief.what_happened || a.brief.recommendation) && <div className="mt-2 rounded-md bg-[#141926] border border-[#222838] p-2.5 space-y-1.5" data-testid={`copilot-brief-${a.id}`}>
                    <div className="text-[10px] uppercase tracking-wider text-blue-400 font-medium">{t("copilot.aiBrief")}</div>
                    {a.brief.what_happened && <p className="text-xs text-gray-300"><span className="text-gray-500">{t("copilot.whatHappened")} </span>{a.brief.what_happened}</p>}
                    {a.brief.what_noticed && <p className="text-xs text-gray-300"><span className="text-gray-500">{t("copilot.whatTheAINoticed")} </span>{a.brief.what_noticed}</p>}
                    {a.brief.recommendation && <p className="text-xs text-gray-300"><span className="text-gray-500">{t("copilot.recommendation")} </span><span className="text-emerald-400">{a.brief.recommendation}</span></p>}
                    {a.brief.reasoning && <p className="text-xs text-gray-500">{a.brief.reasoning}</p>}
                    {a.brief.confidence && <p className="text-[10px] text-gray-600">{t("copilot.confidence")} <span className={`font-medium ${a.brief.confidence === "high" ? "text-emerald-400" : a.brief.confidence === "medium" ? "text-amber-400" : "text-gray-400"}`}>{a.brief.confidence}</span>
                      {a.brief.confidence_basis ? ` — ${a.brief.confidence_basis}` : ""}</p>}
                  </div>}
                {a.result && <p className="mt-1 text-xs text-emerald-400">{a.result}</p>}
                {a.decided_by && <p className="mt-1 text-[10px] text-gray-600">{t("copilot.by")} {a.decided_by}{a.decided_at ? ` · ${fmtDate(a.decided_at.slice(0, 10))}` : ""}</p>}
                {a.status === "pending" && canManage && <div className="mt-2 flex gap-2">
                    <Button data-testid={`copilot-approve-${a.id}`} size="sm" onClick={() => decide(a, true)} className="bg-amber-600 hover:bg-amber-500 text-black h-7">
                      <Check size={13} className="me-1" /> {t("copilot.approveRun")}</Button>
                    <Button data-testid={`copilot-reject-${a.id}`} size="sm" variant="outline" onClick={() => decide(a, false)} className="border-[#222838] h-7">
                      <X size={13} className="me-1" /> {t("copilot.dismiss")}</Button>
                  </div>}
              </div>)}
          </div>
        </Card>
        </PlanGate>
      </div>
    </div>;
}
