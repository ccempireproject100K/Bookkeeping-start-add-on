import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, dollars, fmtDate, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, Upload, Landmark, CreditCard, ArrowLeftRight, Wand2, CheckCircle2, Trash2, RefreshCw, Unplug } from "lucide-react";
import { useTranslation } from "react-i18next";
import PlanGate from "@/components/PlanGate";
const STATUS_CLS = {
  unmatched: "bg-gray-500/10 text-gray-400 border-gray-500/20",
  cleared: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  matched: "bg-sky-500/10 text-sky-400 border-sky-500/20"
};
export default function Banking() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [accounts, setAccounts] = useState(null);
  const [coa, setCoa] = useState([]);
  const [rules, setRules] = useState([]);
  const [recons, setRecons] = useState([]);
  const [selected, setSelected] = useState(null);
  const [txns, setTxns] = useState([]);
  const [tab, setTab] = useState("transactions");
  const [addOpen, setAddOpen] = useState(false);
  const [acctForm, setAcctForm] = useState({
    name: "",
    kind: "bank",
    institution: ""
  });
  const [importOpen, setImportOpen] = useState(false);
  const [csvText, setCsvText] = useState("");
  const [catTxn, setCatTxn] = useState(null);
  const [catCode, setCatCode] = useState("");
  const [xferOpen, setXferOpen] = useState(false);
  const [xfer, setXfer] = useState({
    from_account_id: "",
    to_account_id: "",
    amount: ""
  });
  const [ruleForm, setRuleForm] = useState({
    contains: "",
    category_code: ""
  });
  const [recOpen, setRecOpen] = useState(false);
  const [recForm, setRecForm] = useState({
    statement_date: today(),
    statement_ending_balance: "",
    txn_ids: []
  });
  const [feed, setFeed] = useState(null);
  const [feedBusy, setFeedBusy] = useState(false);
  const [manOpen, setManOpen] = useState(false);
  const [manForm, setManForm] = useState({
    txn_date: today(),
    description: "",
    amount: "",
    direction: "in"
  });
  const [matchTxn, setMatchTxn] = useState(null);
  const [candidates, setCandidates] = useState([]);
  const [matchId, setMatchId] = useState("");
  const loadAccounts = () => api.get("/banking/accounts").then(a => {
    setAccounts(a);
    if (a.length && !selected) setSelected(a[0].id);
  }).catch(() => setAccounts([]));
  const loadTxns = id => api.get(`/banking/transactions?bank_account_id=${id}`).then(setTxns).catch(() => setTxns([]));
  const loadFeed = () => api.get("/bankfeed/status").then(setFeed).catch(() => setFeed({
    configured: false,
    connections: []
  }));
  const loadPlaid = () => new Promise((res, rej) => {
    if (window.Plaid) return res();
    const s = document.createElement("script");
    s.src = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
    s.onload = () => res();
    s.onerror = () => rej(new Error("Could not load Plaid Link"));
    document.head.appendChild(s);
  });
  const connectBank = async () => {
    setFeedBusy(true);
    try {
      const {
        link_token
      } = await api.post("/bankfeed/link-token", {});
      await loadPlaid();
      const handler = window.Plaid.create({
        token: link_token,
        onSuccess: async public_token => {
          try {
            await api.post("/bankfeed/exchange", {
              public_token
            });
            toast.success(t("banking.bankConnectedSyncingNow"));
            await api.post("/bankfeed/sync", {});
            loadFeed();
            loadAccounts();
            if (selected) loadTxns(selected);
          } catch (e) {
            toast.error(errMsg(e));
          }
        },
        onExit: () => {/* user closed Link */}
      });
      handler.open();
    } catch (e) {
      toast.error(errMsg(e));
    }
    setFeedBusy(false);
  };
  const syncFeed = async () => {
    setFeedBusy(true);
    try {
      const r = await api.post("/bankfeed/sync", {});
      toast.success(`Synced: ${r.added} new, ${r.duplicates} duplicate(s) skipped`);
      loadFeed();
      if (selected) loadTxns(selected);
    } catch (e) {
      toast.error(errMsg(e));
    }
    setFeedBusy(false);
  };
  const disconnectFeed = async id => {
    if (!confirm("Disconnect this bank? Already-imported transactions stay in your books.")) return;
    try {
      await api.del(`/bankfeed/connection/${id}`);
      toast.success(t("banking.disconnected"));
      loadFeed();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  useEffect(() => {
    loadAccounts();
    loadFeed();
    api.get("/ledger/accounts").then(setCoa).catch(() => {});
    api.get("/banking/rules").then(setRules).catch(() => {});
  }, []);
  useEffect(() => {
    if (selected) {
      loadTxns(selected);
      api.get(`/banking/reconciliations?bank_account_id=${selected}`).then(setRecons).catch(() => {});
    }
  }, [selected]);
  const addAccount = async () => {
    try {
      await api.post("/banking/accounts", acctForm);
      toast.success(t("banking.accountAdded"));
      setAddOpen(false);
      setAcctForm({
        name: "",
        kind: "bank",
        institution: ""
      });
      loadAccounts();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doImport = async () => {
    try {
      const r = await api.post("/banking/import", {
        bank_account_id: selected,
        csv_text: csvText
      });
      toast.success(`Imported ${r.imported}, skipped ${r.duplicates} duplicate(s)`);
      setImportOpen(false);
      setCsvText("");
      loadTxns(selected);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const categorize = async () => {
    try {
      await api.post(`/banking/transactions/${catTxn.id}/categorize`, {
        category_code: catCode
      });
      toast.success(t("banking.transactionPostedToLedger"));
      setCatTxn(null);
      setCatCode("");
      loadTxns(selected);
      loadAccounts();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doTransfer = async () => {
    try {
      await api.post("/banking/transfer", {
        ...xfer,
        amount: parseFloat(xfer.amount) || 0
      });
      toast.success(t("banking.transferPosted"));
      setXferOpen(false);
      setXfer({
        from_account_id: "",
        to_account_id: "",
        amount: ""
      });
      loadAccounts();
      loadTxns(selected);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doAddManual = async () => {
    const amt = parseFloat(manForm.amount) || 0;
    if (!manForm.description.trim() || !amt) {
      toast.error(t("banking.enterADescriptionAndAnAmount"));
      return;
    }
    try {
      const signed = manForm.direction === "in" ? Math.abs(amt) : -Math.abs(amt);
      await api.post("/banking/transactions", {
        bank_account_id: selected,
        txn_date: manForm.txn_date,
        description: manForm.description.trim(),
        amount: signed
      });
      toast.success(t("banking.transactionAdded"));
      setManOpen(false);
      setManForm({
        txn_date: today(),
        description: "",
        amount: "",
        direction: "in"
      });
      loadTxns(selected);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const openMatch = async t => {
    setMatchTxn(t);
    setMatchId("");
    setCandidates([]);
    try {
      const entries = await api.get("/ledger/journal-entries?status=posted&limit=200");
      const target = Math.abs(t.amount_cents);
      const tDate = new Date(t.txn_date).getTime();
      const ranked = entries.filter(e => e.source !== "bank_txn") // already posted FROM a bank txn — never a match candidate
      .map(e => {
        const amtDiff = Math.abs((e.total_cents || 0) - target);
        const dayDiff = Math.abs((new Date(e.entry_date).getTime() - tDate) / 86400000);
        return {
          ...e,
          _amtDiff: amtDiff,
          _score: amtDiff + dayDiff * 100
        };
      }).sort((a, b) => a._score - b._score).slice(0, 25);
      setCandidates(ranked);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doMatch = async () => {
    if (!matchId) {
      toast.error(t("banking.pickAJournalEntryToMatch"));
      return;
    }
    try {
      const r = await api.post(`/banking/transactions/${matchTxn.id}/match?entry_id=${matchId}`, {});
      toast.success(`Matched to entry ${r.entry_number}`);
      setMatchTxn(null);
      setMatchId("");
      loadTxns(selected);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const addRule = async () => {
    try {
      await api.post("/banking/rules", ruleForm);
      toast.success(t("banking.ruleAdded"));
      setRuleForm({
        contains: "",
        category_code: ""
      });
      api.get("/banking/rules").then(setRules);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const delRule = async id => {
    try {
      await api.del(`/banking/rules/${id}`);
      api.get("/banking/rules").then(setRules);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const reconcile = async () => {
    try {
      const r = await api.post("/banking/reconciliations", {
        bank_account_id: selected,
        statement_date: recForm.statement_date,
        statement_ending_balance: parseFloat(recForm.statement_ending_balance) || 0,
        txn_ids: recForm.txn_ids
      });
      if (r.status === "completed") toast.success(t("banking.reconciledBalanced"));else toast.warning(`Off by ${money(r.difference_cents)} — adjust selection`);
      setRecOpen(false);
      loadTxns(selected);
      api.get(`/banking/reconciliations?bank_account_id=${selected}`).then(setRecons);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!accounts) return <Loading />;
  const acct = accounts.find(a => a.id === selected);
  const catOptions = coa.filter(c => !c.archived);
  return <div className="animate-rise">
      <PageHeader title={t("banking.banking")} subtitle="Accounts, imports, categorization & reconciliation" testid="banking-page">
        {canManage && <>
          <Button data-testid="btn-transfer" variant="outline" onClick={() => setXferOpen(true)} className="border-[#222838]"><ArrowLeftRight size={15} className="mr-1" />{t("banking.transfer")}</Button>
          <Button data-testid="btn-add-account" onClick={() => setAddOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("banking.addAccount")}</Button>
        </>}
      </PageHeader>

      {/* Live bank feeds (Plaid) — Medium plan and up */}
      {feed && <PlanGate feature="bank_feeds">
        <Card className="mb-6" data-testid="bankfeed-panel">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex-1 min-w-56">
              <div className="text-xs uppercase tracking-wider text-gray-500">{t("banking.liveBankFeeds")}</div>
              {feed.configured ? <div className="text-sm text-gray-300 mt-1">
                  {feed.connections.length === 0 ? "No banks connected yet. Connect one and new transactions will appear in the register below for review." : `${feed.connections.length} bank connection(s) · ${feed.env}`}
                </div> : <div className="text-sm text-gray-400 mt-1">{t("banking.connectABankOnceAndTransactions")} <code className="font-mono text-xs text-amber-400">{t("banking.plaidCLIENTID")}</code>,{" "}
                  <code className="font-mono text-xs text-amber-400">{t("banking.plaidSECRET")}</code> {t("banking.and")}{" "}
                  <code className="font-mono text-xs text-amber-400">{t("banking.tokenENCRYPTIONKEY")}</code> {t("banking.onTheServerSee")} <code className="font-mono text-xs text-amber-400">{t("banking.plaidSETUPMd")}</code>{t("banking.thenComeBackHere")}</div>}
            </div>
            {feed.configured && canManage && <div className="flex gap-2">
                {feed.connections.length > 0 && <Button data-testid="bankfeed-sync" variant="outline" size="sm" disabled={feedBusy} onClick={syncFeed} className="border-[#222838]">
                    <RefreshCw size={14} className="mr-1" /> {t("banking.syncNow")}</Button>}
                <Button data-testid="bankfeed-connect" size="sm" disabled={feedBusy} onClick={connectBank} className="bg-amber-600 hover:bg-amber-500 text-black">
                  <Landmark size={14} className="mr-1" /> {t("banking.connectBank")}</Button>
              </div>}
          </div>
          {feed.configured && feed.connections.length > 0 && <div className="mt-3 space-y-2">
              {feed.connections.map(c => <div key={c.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-[#0F1218] border border-[#222838] px-4 py-2 text-sm">
                  <div>
                    <div className="text-gray-100 font-medium">{(c.accounts || []).map(a => `${a.name} ••${a.mask || ""}`).join(", ") || "Bank account"}</div>
                    <div className="text-xs text-gray-500">{t("banking.lastSync")} {c.last_sync ? new Date(c.last_sync).toLocaleString() : "never"}</div>
                  </div>
                  {canManage && <button onClick={() => disconnectFeed(c.id)} data-testid={`bankfeed-disconnect-${c.id}`} className="text-gray-600 hover:text-rose-400 text-xs flex items-center gap-1">
                      <Unplug size={13} /> {t("banking.disconnect")}</button>}
                </div>)}
            </div>}
        </Card>
      </PlanGate>}

      {/* Account cards */}
      <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-6" data-testid="bank-account-cards">
        {accounts.map(a => <button key={a.id} data-testid={`bank-acct-${a.id}`} onClick={() => setSelected(a.id)} className={`text-left card-surface p-4 transition-all ${selected === a.id ? "ring-2 ring-amber-500/50" : "hover:bg-[#161b25]"}`}>
            <div className="flex items-center gap-2 text-gray-400 text-xs uppercase tracking-wide">
              {a.kind === "credit_card" ? <CreditCard size={14} /> : <Landmark size={14} />}{a.kind === "credit_card" ? "Credit Card" : "Bank"}
            </div>
            <div className="font-display font-semibold text-gray-100 mt-1">{a.name}</div>
            <div className="text-2xl font-mono mt-2 text-amber-400">{dollars(a.gl_balance)}</div>
            <div className="text-xs text-gray-500 mt-1">{a.unreconciled_count} unreconciled ·  {a.gl_name}</div>
          </button>)}
        {accounts.length === 0 && <div className="text-gray-500 text-sm col-span-full py-8 text-center">{t("banking.noBankAccountsYetAddOne")}</div>}
      </div>

      {acct && <>
          <div className="flex items-center gap-2 mb-4">
            {["transactions", "rules", "reconciliations"].map(t => <button key={t} data-testid={`tab-${t}`} onClick={() => setTab(t)} className={`px-3 py-1.5 rounded-lg text-sm capitalize ${tab === t ? "bg-amber-500/10 text-amber-400 border border-amber-500/30" : "text-gray-400 border border-transparent hover:bg-[#161b25]"}`}>{t}</button>)}
            <div className="flex-1" />
            {canManage && tab === "transactions" && <>
              <Button data-testid="btn-add-txn" variant="outline" size="sm" onClick={() => {
            setManForm({
              txn_date: today(),
              description: "",
              amount: "",
              direction: "in"
            });
            setManOpen(true);
          }} className="border-[#222838]"><Plus size={14} className="mr-1" />{t("banking.addTransaction")}</Button>
              <Button data-testid="btn-import" variant="outline" size="sm" onClick={() => setImportOpen(true)} className="border-[#222838]"><Upload size={14} className="mr-1" />{t("banking.importCSV")}</Button>
              <Button data-testid="btn-reconcile" size="sm" onClick={() => {
            setRecForm({
              statement_date: today(),
              statement_ending_balance: "",
              txn_ids: txns.filter(t => t.status !== "unmatched" && !t.reconciled).map(t => t.id)
            });
            setRecOpen(true);
          }} className="bg-emerald-600 hover:bg-emerald-500 text-black"><CheckCircle2 size={14} className="mr-1" />{t("banking.reconcile")}</Button>
            </>}
          </div>

          {tab === "transactions" && <Card className="p-0 overflow-hidden">
              <table className="w-full text-sm" data-testid="table-bank-txns">
                <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-4 py-3 text-left font-medium">{t("banking.date")}</th>
                  <th className="px-4 py-3 text-left font-medium">{t("banking.description")}</th>
                  <th className="px-4 py-3 text-right font-medium">{t("banking.amount")}</th>
                  <th className="px-4 py-3 text-left font-medium">{t("banking.category")}</th>
                  <th className="px-4 py-3 text-left font-medium">{t("banking.status")}</th>
                  {canManage && <th className="px-4 py-3 text-right font-medium">{t("banking.action")}</th>}
                </tr></thead>
                <tbody>
                  {txns.map(t => <tr key={t.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                      <td className="px-4 py-3 text-gray-400">{fmtDate(t.txn_date)}</td>
                      <td className="px-4 py-3 text-gray-100">{t.description}{t.suggested_code && !t.category_code && <span className="ml-2 text-xs text-amber-500/70">{t("banking.rule")}{t.suggested_code}</span>}</td>
                      <td className={`px-4 py-3 text-right font-mono ${t.amount_cents < 0 ? "text-rose-400" : "text-emerald-400"}`}>{money(t.amount_cents)}</td>
                      <td className="px-4 py-3 text-gray-400">{t.category_code || "—"}</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[t.status] || ""}`}>{t.reconciled ? "reconciled" : t.status}</span></td>
                      {canManage && <td className="px-4 py-3 text-right">
                        {t.status === "unmatched" && <div className="flex justify-end gap-1">
                            <Button size="sm" variant="ghost" className="h-7 text-sky-400 hover:text-sky-300" data-testid={`match-${t.id}`} onClick={() => openMatch(t)}>{t("banking.match")}</Button>
                            <Button size="sm" className="h-7 bg-amber-600 hover:bg-amber-500 text-black" data-testid={`categorize-${t.id}`} onClick={() => {
                    setCatTxn(t);
                    setCatCode(t.suggested_code || "");
                  }}>{t("banking.categorize")}</Button>
                          </div>}
                      </td>}
                    </tr>)}
                  {txns.length === 0 && <tr><td colSpan={canManage ? 6 : 5} className="px-5 py-12 text-center text-gray-500">{t("banking.noTransactionsImportACSVOr")}</td></tr>}
                </tbody>
              </table>
            </Card>}

          {tab === "rules" && <Card>
              {canManage && <div className="flex flex-wrap items-end gap-3 mb-4">
                  <div><Label className="text-xs">{t("banking.whenDescriptionContains")}</Label><Input data-testid="rule-contains" value={ruleForm.contains} onChange={e => setRuleForm({
              ...ruleForm,
              contains: e.target.value
            })} className="bg-[#0F1218] border-[#222838] w-56" placeholder={t("banking.eGHomeDepot")} /></div>
                  <div className="w-56"><Label className="text-xs">{t("banking.categorizeAs")}</Label>
                    <Select value={ruleForm.category_code} onValueChange={v => setRuleForm({
              ...ruleForm,
              category_code: v
            })}>
                      <SelectTrigger data-testid="rule-category" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("banking.account")} /></SelectTrigger>
                      <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">{catOptions.map(c => <SelectItem key={c.code} value={c.code}>{c.code} · {c.name}</SelectItem>)}</SelectContent>
                    </Select></div>
                  <Button data-testid="btn-add-rule" onClick={addRule} className="bg-amber-600 hover:bg-amber-500 text-black"><Wand2 size={14} className="mr-1" />{t("banking.addRule")}</Button>
                </div>}
              <div className="space-y-2" data-testid="rules-list">
                {rules.map(r => <div key={r.id} className="flex items-center justify-between bg-[#0F1218] border border-[#222838] rounded-lg px-4 py-2 text-sm">
                    <span className="text-gray-300">“{r.contains}” → <span className="text-amber-400">{r.category_code} {r.category_name}</span></span>
                    {canManage && <button onClick={() => delRule(r.id)} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
                  </div>)}
                {rules.length === 0 && <div className="text-gray-500 text-sm py-6 text-center">{t("banking.noRulesYetRulesAutoSuggest")}</div>}
              </div>
            </Card>}

          {tab === "reconciliations" && <Card className="p-0 overflow-hidden">
              <table className="w-full text-sm" data-testid="table-recons">
                <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-4 py-3 text-left font-medium">{t("banking.statementDate")}</th>
                  <th className="px-4 py-3 text-right font-medium">{t("banking.endingBalance")}</th>
                  <th className="px-4 py-3 text-right font-medium">{t("banking.cleared")}</th>
                  <th className="px-4 py-3 text-right font-medium">{t("banking.difference")}</th>
                  <th className="px-4 py-3 text-left font-medium">{t("banking.status")}</th>
                </tr></thead>
                <tbody>
                  {recons.map(r => <tr key={r.id} className="border-b border-[#171c26]">
                      <td className="px-4 py-3 text-gray-300">{fmtDate(r.statement_date)}</td>
                      <td className="px-4 py-3 text-right font-mono text-gray-200">{money(r.statement_ending_cents)}</td>
                      <td className="px-4 py-3 text-right font-mono text-gray-400">{money(r.cleared_cents)}</td>
                      <td className={`px-4 py-3 text-right font-mono ${r.difference_cents === 0 ? "text-emerald-400" : "text-rose-400"}`}>{money(r.difference_cents)}</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${r.status === "completed" ? STATUS_CLS.cleared : STATUS_CLS.unmatched}`}>{r.status}</span></td>
                    </tr>)}
                  {recons.length === 0 && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">{t("banking.noReconciliationsYet")}</td></tr>}
                </tbody>
              </table>
            </Card>}
        </>}

      {/* Add account dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("banking.addBankCardAccount")}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("banking.name")}</Label><Input data-testid="acct-name" value={acctForm.name} onChange={e => setAcctForm({
            ...acctForm,
            name: e.target.value
          })} className="bg-[#0F1218] border-[#222838]" /></div>
          <div><Label className="text-xs">{t("banking.type")}</Label>
            <Select value={acctForm.kind} onValueChange={v => setAcctForm({
            ...acctForm,
            kind: v
          })}>
              <SelectTrigger data-testid="acct-kind" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="bank">{t("banking.bank")}</SelectItem><SelectItem value="credit_card">{t("banking.creditCard")}</SelectItem></SelectContent>
            </Select></div>
          <div><Label className="text-xs">{t("banking.institution")}</Label><Input data-testid="acct-institution" value={acctForm.institution} onChange={e => setAcctForm({
            ...acctForm,
            institution: e.target.value
          })} className="bg-[#0F1218] border-[#222838]" /></div>
          <DialogFooter><Button data-testid="acct-save" onClick={addAccount} className="bg-amber-600 hover:bg-amber-500 text-black">{t("banking.addAccount")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Import dialog */}
      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("banking.importTransactionsCSV")}</DialogTitle></DialogHeader>
          <p className="text-xs text-gray-500">{t("banking.pasteCSVWithHeadersLike")} <code>{t("banking.dateDescriptionAmount")}</code> {t("banking.negativeMoneyOutDuplicatesAreAuto")}</p>
          <Textarea data-testid="import-csv" value={csvText} onChange={e => setCsvText(e.target.value)} rows={8} className="bg-[#0F1218] border-[#222838] font-mono text-xs" placeholder={"Date,Description,Amount\n2026-06-01,Home Depot,-250.00\n2026-06-02,Customer deposit,1200.00"} />
          <DialogFooter><Button data-testid="import-save" onClick={doImport} className="bg-amber-600 hover:bg-amber-500 text-black">{t("banking.import")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Categorize dialog */}
      <Dialog open={!!catTxn} onOpenChange={o => !o && setCatTxn(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("banking.categorizeTransaction")}</DialogTitle></DialogHeader>
          {catTxn && <div className="text-sm text-gray-400">{catTxn.description} · <span className="font-mono">{money(catTxn.amount_cents)}</span></div>}
          <div><Label className="text-xs">{t("banking.account")}</Label>
            <Select value={catCode} onValueChange={setCatCode}>
              <SelectTrigger data-testid="cat-select" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("banking.chooseAccount")} /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-80">{catOptions.map(c => <SelectItem key={c.code} value={c.code}>{c.code} · {c.name}</SelectItem>)}</SelectContent>
            </Select></div>
          <DialogFooter><Button data-testid="cat-save" disabled={!catCode} onClick={categorize} className="bg-amber-600 hover:bg-amber-500 text-black">{t("banking.postToLedger")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Transfer dialog */}
      <Dialog open={xferOpen} onOpenChange={setXferOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("banking.transferBetweenAccounts")}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("banking.from")}</Label>
            <Select value={xfer.from_account_id} onValueChange={v => setXfer({
            ...xfer,
            from_account_id: v
          })}>
              <SelectTrigger data-testid="xfer-from" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("banking.account")} /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">{accounts.map(a => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}</SelectContent>
            </Select></div>
          <div><Label className="text-xs">{t("banking.to")}</Label>
            <Select value={xfer.to_account_id} onValueChange={v => setXfer({
            ...xfer,
            to_account_id: v
          })}>
              <SelectTrigger data-testid="xfer-to" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("banking.account")} /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">{accounts.map(a => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}</SelectContent>
            </Select></div>
          <div><Label className="text-xs">{t("banking.amount2")}</Label><Input data-testid="xfer-amount" value={xfer.amount} onChange={e => setXfer({
            ...xfer,
            amount: e.target.value
          })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <DialogFooter><Button data-testid="xfer-save" onClick={doTransfer} className="bg-amber-600 hover:bg-amber-500 text-black">{t("banking.transfer")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Manual transaction dialog */}
      <Dialog open={manOpen} onOpenChange={setManOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("banking.addTransaction2")} {acct?.name}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("banking.date")}</Label><Input data-testid="mantxn-date" type="date" value={manForm.txn_date} onChange={e => setManForm({
              ...manForm,
              txn_date: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("banking.direction")}</Label>
              <Select value={manForm.direction} onValueChange={v => setManForm({
              ...manForm,
              direction: v
            })}>
                <SelectTrigger data-testid="mantxn-direction" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="in">{t("banking.moneyIn")}</SelectItem><SelectItem value="out">{t("banking.moneyOut")}</SelectItem></SelectContent>
              </Select></div>
          </div>
          <div><Label className="text-xs">{t("banking.description")}</Label><Input data-testid="mantxn-desc" value={manForm.description} onChange={e => setManForm({
            ...manForm,
            description: e.target.value
          })} placeholder={t("banking.eGSupplyRunHomeDepot")} className="bg-[#0F1218] border-[#222838]" /></div>
          <div><Label className="text-xs">{t("banking.amount2")}</Label><Input data-testid="mantxn-amount" value={manForm.amount} onChange={e => setManForm({
            ...manForm,
            amount: e.target.value
          })} placeholder="0.00" className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <DialogFooter><Button data-testid="mantxn-save" onClick={doAddManual} className="bg-amber-600 hover:bg-amber-500 text-black">{t("banking.addTransaction")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Match-to-entry dialog */}
      <Dialog open={!!matchTxn} onOpenChange={o => !o && setMatchTxn(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("banking.matchBankTransaction")}</DialogTitle></DialogHeader>
          {matchTxn && <div className="text-sm text-gray-400">{matchTxn.description} · <span className="font-mono">{money(matchTxn.amount_cents)}</span> · {fmtDate(matchTxn.txn_date)}</div>}
          <p className="text-xs text-gray-500">{t("banking.linkThisToAJournalEntry")}</p>
          <div className="max-h-64 overflow-y-auto space-y-1" data-testid="match-candidates">
            {candidates.map((c, i) => <button key={c.id} onClick={() => setMatchId(c.id)} data-testid={`match-pick-${c.id}`} className={`w-full text-left px-3 py-2 rounded-lg border text-sm transition-colors ${matchId === c.id ? "border-amber-500/60 bg-amber-500/10" : "border-[#222838] hover:bg-[#161b25]"}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-gray-100 font-mono">{c.number}</span>
                  <span className="font-mono text-gray-300">{money(c.total_cents)}</span>
                </div>
                <div className="flex items-center justify-between gap-2 mt-0.5">
                  <span className="text-xs text-gray-500 truncate">{c.memo || c.source}</span>
                  <span className="text-xs text-gray-500 whitespace-nowrap">{fmtDate(c.entry_date)}{i === 0 && c._amtDiff === 0 && <span className="ml-2 text-emerald-400">{t("banking.bestMatch")}</span>}</span>
                </div>
              </button>)}
            {candidates.length === 0 && <div className="text-gray-500 text-sm py-6 text-center">{t("banking.noCandidateEntriesFound")}</div>}
          </div>
          <DialogFooter><Button data-testid="match-save" disabled={!matchId} onClick={doMatch} className="bg-sky-600 hover:bg-sky-500 text-black">{t("banking.matchSelected")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Reconcile dialog */}
      <Dialog open={recOpen} onOpenChange={setRecOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("banking.reconcile")} {acct?.name}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("banking.statementDate2")}</Label><Input data-testid="rec-date" type="date" value={recForm.statement_date} onChange={e => setRecForm({
              ...recForm,
              statement_date: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("banking.endingBalance2")}</Label><Input data-testid="rec-ending" value={recForm.statement_ending_balance} onChange={e => setRecForm({
              ...recForm,
              statement_ending_balance: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <p className="text-xs text-gray-500">{t("banking.including")} {recForm.txn_ids.length} {t("banking.clearedMatchedTransactionSTheReconciliation")}</p>
          <DialogFooter><Button data-testid="rec-save" onClick={reconcile} className="bg-emerald-600 hover:bg-emerald-500 text-black">{t("banking.finishReconciliation")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
