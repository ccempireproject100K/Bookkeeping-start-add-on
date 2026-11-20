import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, moneyFor } from "@/lib/format";
import { PageHeader, Card, Empty, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import {
  Home, Layers, Lock, Mail, PiggyBank, Plus, ShieldCheck, Sparkles,
  Target, Trash2, TrendingUp, Wallet, Wand2, X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

/* Household money (personal-finance layer, slice 2): the customer-facing
 * home for the /personal API. Renders ONLY inside a household workspace —
 * the backend 403s every personal endpoint for business tenants, and this
 * page shows the create/switch explanation instead of personal data when
 * the active workspace is a business. One login can hold both: business
 * books and a household, switched from the workspace menu. */

function BalanceBars({ points }) {
  if (!points || points.length < 2) return null;
  const step = Math.max(1, Math.ceil(points.length / 72));
  const sampled = points.filter((_, i) => i % step === 0);
  const vals = sampled.map((p) => p.net_worth_cents);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const span = hi - lo || 1;
  return (
    <div>
      <div className="flex items-end gap-[2px] h-28">
        {sampled.map((p) => {
          const h = 8 + ((p.net_worth_cents - lo) / span) * 92;
          const neg = p.net_worth_cents < 0;
          return (
            <div
              key={p.date}
              title={`${p.date}: ${money(p.net_worth_cents)}`}
              className={`flex-1 rounded-t ${neg ? "bg-red-500/70" : "bg-emerald-400/70"}`}
              style={{ height: `${h}%` }}
            />
          );
        })}
      </div>
      <div className="flex justify-between text-xs text-gray-500 mt-1">
        <span>{sampled[0]?.date}</span>
        <span>{sampled[sampled.length - 1]?.date}</span>
      </div>
    </div>
  );
}

function BudgetBar({ row }) {
  const pctUsed =
    row.budgeted_cents > 0
      ? Math.round((row.spent_cents / row.budgeted_cents) * 100)
      : row.spent_cents > 0
        ? 100
        : 0;
  const tone =
    pctUsed > 100 ? "bg-red-500" : pctUsed > 80 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className="py-2 border-b border-white/5 last:border-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-gray-200">{row.category}</span>
        <span className="text-xs text-gray-400">
          {money(row.spent_cents)} of {money(row.budgeted_cents)}
          {row.rollover && (
            <span className="ml-2 rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-300">
              rollover{row.carried_in_cents ? ` · ${money(row.carried_in_cents)} carried in` : ""}
            </span>
          )}
        </span>
      </div>
      <div className="h-1.5 mt-1.5 overflow-hidden rounded-full bg-[#222838]">
        <div className={`h-full ${tone}`} style={{ width: `${Math.min(pctUsed, 100)}%` }} />
      </div>
      <div className="text-xs mt-1 text-gray-500">
        {money(row.remaining_cents)} remaining
      </div>
    </div>
  );
}

/* Slice 3 (budget-app fold-in): an envelope is a persistent pot for
 * one category — its own cycle, a health state (green while more than
 * 20% remains, yellow at 20% or less, red when empty or overspent),
 * and unspent money rolling into the next cycle when rollover is on. */
const ENVELOPE_TONES = {
  green: { bar: "bg-emerald-500", chip: "bg-emerald-500/15 text-emerald-300", label: "Healthy" },
  yellow: { bar: "bg-amber-500", chip: "bg-amber-500/15 text-amber-300", label: "Running low" },
  red: { bar: "bg-red-500", chip: "bg-red-500/15 text-red-300", label: "Empty" },
};

function EnvelopeBar({ row, onDelete }) {
  const tone = ENVELOPE_TONES[row.state] || ENVELOPE_TONES.green;
  const pctUsed =
    row.effective_allocated_cents > 0
      ? Math.max(0, Math.min(100, Math.round((row.spent_cents / row.effective_allocated_cents) * 100)))
      : row.spent_cents > 0
        ? 100
        : 0;
  return (
    <div className="py-2 border-b border-white/5 last:border-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-gray-200">
          {row.name}
          <span className="ml-2 rounded bg-white/5 px-1.5 py-0.5 text-xs text-gray-400">{row.category}</span>
          <span className={`ml-2 rounded px-1.5 py-0.5 text-xs ${tone.chip}`}>{tone.label}</span>
        </span>
        <span className="flex items-center gap-2 text-xs text-gray-400">
          {money(row.spent_cents)} of {money(row.effective_allocated_cents)}
          {onDelete && (
            <button className="text-gray-500 hover:text-red-300" onClick={onDelete} aria-label="Delete envelope">
              <Trash2 size={14} />
            </button>
          )}
        </span>
      </div>
      <div className="h-1.5 mt-1.5 overflow-hidden rounded-full bg-[#222838]">
        <div className={`h-full ${tone.bar}`} style={{ width: `${pctUsed}%` }} />
      </div>
      <div className="text-xs mt-1 text-gray-500">
        {money(row.remaining_cents)} remaining · cycle {row.cycle_start} → {row.cycle_end}
        {row.rollover && row.carried_in_cents !== 0 && (
          <> · {money(row.carried_in_cents)} carried in</>
        )}
        {row.members?.length > 0 && <> · shared with {row.members.join(", ")}</>}
      </div>
    </div>
  );
}

function DueBadge({ bill }) {
  if (bill.due_state === "overdue")
    return <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-xs text-red-300">Overdue {Math.abs(bill.days_until_due)}d</span>;
  if (bill.due_state === "due_soon")
    return <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-300">Due in {bill.days_until_due}d</span>;
  return <span className="rounded bg-white/5 px-1.5 py-0.5 text-xs text-gray-400">{bill.next_due_date}</span>;
}

export default function Household() {
  const { t } = useTranslation();
  const { user, refresh } = useAuth();
  const isHousehold = user?.tenant?.kind === "household";
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [suggestions, setSuggestions] = useState(null);
  const [schedule, setSchedule] = useState(null);
  // Slice 3 fold-in surfaces (envelopes, safe-to-spend, savings
  // methods, vaults, age of money) load from their own endpoints so
  // the slice-2 summary contract stays exactly as it was.
  const [foldin, setFoldin] = useState(null);
  const [envSuggestions, setEnvSuggestions] = useState(null);
  const [forms, setForms] = useState({
    budget: { category: "", amount: "", rollover: true },
    bill: { name: "", amount: "", cadence: "monthly", next_due_date: "" },
    goal: { name: "", target: "", amount: "", cadence: "monthly", start: "",
            from_account: "", to_account: "" },
    asset: { name: "", kind: "other_asset", value: "", currency: "" },
    envelope: { name: "", category: "", amount: "", cycle_start_day: "1",
                rollover: true },
    paycheck: { amount: "" },
    vault: { name: "", amount: "", delay_hours: "48" },
    vaultMove: { vault_id: "", amount: "" },
  });

  const load = useCallback(async () => {
    if (!isHousehold) {
      setLoading(false);
      return;
    }
    try {
      // Catch up self-funding goals first so balances shown are current;
      // the hourly scheduler does the same server-side.
      await api.post("/personal/goals/fund-due", {}).catch(() => {});
      setSummary(await api.get("/personal/summary"));
      const [envelopes, safe, methods, vaults, age] = await Promise.all([
        api.get("/personal/envelopes").catch(() => []),
        api.get("/personal/safe-to-spend").catch(() => null),
        api.get("/personal/savings-methods").catch(() => []),
        api.get("/personal/vaults").catch(() => []),
        api.get("/personal/age-of-money").catch(() => null),
      ]);
      setFoldin({
        envelopes: Array.isArray(envelopes) ? envelopes : [],
        safe: safe && typeof safe === "object" && safe.parts ? safe : null,
        methods: Array.isArray(methods) ? methods : [],
        vaults: Array.isArray(vaults) ? vaults : [],
        age: age && typeof age === "object" ? age : null,
      });
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [isHousehold]);

  useEffect(() => {
    load();
  }, [load]);

  const createHousehold = async () => {
    try {
      await api.post("/personal/households", {});
      toast.success("Household created — switch to it from the workspace menu.");
      await refresh();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const detect = async () => {
    try {
      const d = await api.get("/personal/recurring-bills/detect");
      setSuggestions(d.suggestions);
      if (!d.suggestions.length) toast.success("No new recurring bills found.");
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const confirmSuggestion = async (s) => {
    try {
      await api.post("/personal/recurring-bills", {
        name: s.suggested_name,
        category: s.category,
        amount_cents: s.amount_cents,
        cadence: s.cadence,
        next_due_date: s.suggested_next_due_date,
        source: "detected",
      });
      setSuggestions((prev) => prev.filter((x) => x !== s));
      toast.success(`Added ${s.suggested_name} to your bills.`);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const showSchedule = async (asset) => {
    try {
      setSchedule(await api.get(`/personal/assets/${asset.id}/schedule`));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const submit = async (kind) => {
    const f = forms[kind];
    try {
      if (kind === "budget") {
        await api.post("/personal/budgets", {
          category: f.category,
          period: new Date().toISOString().slice(0, 7),
          amount_cents: Math.round(parseFloat(f.amount || "0") * 100),
          rollover: !!f.rollover,
        });
      } else if (kind === "bill") {
        await api.post("/personal/recurring-bills", {
          name: f.name,
          amount_cents: Math.round(parseFloat(f.amount || "0") * 100),
          cadence: f.cadence,
          next_due_date: f.next_due_date,
        });
      } else if (kind === "goal") {
        await api.post("/personal/goals", {
          name: f.name,
          target_cents: Math.round(parseFloat(f.target || "0") * 100),
          auto_fund: !!f.amount,
          funding_amount_cents: f.amount
            ? Math.round(parseFloat(f.amount) * 100)
            : null,
          funding_cadence: f.amount ? f.cadence : null,
          funding_start_date: f.amount && f.start ? f.start : null,
          funding_account_id: f.amount && f.from_account ? f.from_account : null,
          funding_destination_account_id:
            f.amount && f.to_account ? f.to_account : null,
        });
      } else if (kind === "asset") {
        await api.post("/personal/assets", {
          name: f.name,
          kind: f.kind,
          value_cents: Math.round(parseFloat(f.value || "0") * 100),
          currency: f.currency || null,
        });
      }
      toast.success("Saved.");
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const remove = async (path) => {
    try {
      await api.del(path);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  // ---- slice 3 fold-in handlers (api.del is the client's delete) ----

  const submitEnvelope = async () => {
    const f = forms.envelope;
    try {
      await api.post("/personal/envelopes", {
        name: f.name,
        category: f.category,
        allocated_cents: Math.round(parseFloat(f.amount || "0") * 100),
        cycle_start_day: Math.min(28, Math.max(1, parseInt(f.cycle_start_day || "1", 10) || 1)),
        rollover: !!f.rollover,
      });
      toast.success("Envelope saved.");
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const suggestEnvelopes = async () => {
    try {
      const paycheck = Math.round(parseFloat(forms.paycheck.amount || "0") * 100);
      if (!paycheck) {
        toast.error("Enter a paycheck amount to split.");
        return;
      }
      const d = await api.get(`/personal/envelopes/suggestions?paycheck_cents=${paycheck}`);
      setEnvSuggestions(d.rows);
      if (!d.rows.length) toast.success("No spending history to suggest from yet.");
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const applySuggestion = (row) => {
    setForms((prev) => ({
      ...prev,
      envelope: { ...prev.envelope, name: row.category, category: row.category,
                  amount: String(row.suggested_cents / 100) },
    }));
  };

  const submitVault = async () => {
    const f = forms.vault;
    try {
      await api.post("/personal/vaults", {
        name: f.name,
        balance_cents: Math.round(parseFloat(f.amount || "0") * 100),
        withdrawal_delay_hours: parseInt(f.delay_hours || "48", 10) || 0,
      });
      toast.success("Vault saved.");
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const vaultMove = async (kind) => {
    const f = forms.vaultMove;
    if (!f.vault_id) {
      toast.error("Pick a vault first.");
      return;
    }
    try {
      await api.post(`/personal/vaults/${f.vault_id}/${kind}`, {
        amount_cents: Math.round(parseFloat(f.amount || "0") * 100),
      });
      toast.success("Saved.");
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const vaultWithdraw = async (vault) => {
    try {
      await api.post(`/personal/vaults/${vault.id}/withdraw`, {});
      toast.success("Withdrawn.");
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const methodEnroll = async (m) => {
    try {
      await api.post(`/personal/savings-methods/${m.key}/enroll`, {});
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const methodAction = async (m) => {
    if (!m.next_action) return;
    try {
      await api.post(`/personal/savings-methods/${m.key}/actions`, {
        action: m.next_action.action,
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const methodUnenroll = async (m) => {
    try {
      await api.del(`/personal/savings-methods/${m.key}`);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const removeFoldin = async (path) => {
    try {
      await api.del(path);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const set = (kind, key) => (e) =>
    setForms((prev) => ({
      ...prev,
      [kind]: {
        ...prev[kind],
        [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value,
      },
    }));

  if (loading) return <Loading />;

  if (!isHousehold) {
    return (
      <div data-testid="household-not-household">
        <PageHeader title={t("household.title")} subtitle={t("household.subtitle")} />
        <Card className="max-w-xl mx-auto text-center">
          <Home className="mx-auto mb-3 text-emerald-400" size={36} />
          <h2 className="font-display text-lg font-semibold text-gray-100">
            {t("household.notHouseholdTitle")}
          </h2>
          <p className="text-sm text-gray-400 mt-2">{t("household.notHouseholdBody")}</p>
          <Button className="mt-4" onClick={createHousehold} data-testid="household-create">
            <Plus size={16} className="mr-1" /> {t("household.create")}
          </Button>
        </Card>
      </div>
    );
  }

  if (!summary) return <Empty>Could not load your household yet.</Empty>;
  const nw = summary.net_worth;
  const base = nw.base_currency || "USD";

  return (
    <div data-testid="household-page">
      <PageHeader title={t("household.title")} subtitle={t("household.subtitle")}>
        <Button variant="outline" onClick={detect} data-testid="household-detect">
          <Wand2 size={16} className="mr-1" /> {t("household.detectBills")}
        </Button>
      </PageHeader>

      {/* Net-worth hero */}
      <Card className="mb-6 bg-gradient-to-br from-emerald-500/10 via-transparent to-amber-500/10">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="text-xs uppercase tracking-wider text-gray-400 flex items-center gap-1">
              <TrendingUp size={14} /> {t("household.netWorth")} ({base})
            </div>
            <div className="font-display text-4xl font-bold text-gray-50 mt-1" data-testid="household-net-worth">
              {moneyFor(nw.net_worth_cents, base)}
            </div>
            <div className="text-sm text-gray-400 mt-1">
              Assets {moneyFor(nw.assets_cents, base)} · Debts{" "}
              {moneyFor(nw.liabilities_cents, base)}
              {nw.fx_pending_count > 0 && (
                <span className="text-amber-300">
                  {" "}· {nw.fx_pending_count} item(s) awaiting an FX rate
                </span>
              )}
            </div>
          </div>
          <div className="text-sm text-gray-300">
            <div>
              {t("household.cashFlow")}:{" "}
              <span className={summary.cash_flow.net_cents >= 0 ? "text-emerald-300" : "text-red-300"}>
                {money(summary.cash_flow.net_cents)}
              </span>
            </div>
            <div className="text-gray-500 text-xs mt-1">
              {t("household.moneyIn")} {money(summary.cash_flow.in_cents)} ·{" "}
              {t("household.moneyOut")} {money(summary.cash_flow.out_cents)}
            </div>
          </div>
        </div>
        <div className="mt-4">
          <BalanceBars points={summary.balance_history?.points} />
          {(!summary.balance_history?.points ||
            summary.balance_history.points.length < 2) && (
            <div className="text-xs text-gray-500" data-testid="household-chart-empty">
              Your balance chart appears here once transactions come in.
            </div>
          )}
        </div>
      </Card>

      {/* Safe to spend (slice 3 fold-in): the family number — balance
          minus bills due before payday, minus envelope promises, minus
          vault locks, with every part shown. */}
      {foldin?.safe && (
        <Card className="mb-6" data-testid="household-safe-to-spend">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="text-xs uppercase tracking-wider text-gray-400 flex items-center gap-1">
                <ShieldCheck size={14} /> {t("household.safeToSpend")}
              </div>
              <div className="font-display text-3xl font-bold text-gray-50 mt-1">
                {money(foldin.safe.safe_to_spend_cents)}
              </div>
              <div className="text-xs text-gray-500 mt-1 max-w-md">
                {foldin.safe.plain_words}
              </div>
              {foldin.age && foldin.age.oldest_unspent_income_date && (
                <div className="text-xs text-gray-500 mt-1">
                  Age of money: {foldin.age.age_days} days — your oldest
                  unspent dollar arrived {foldin.age.oldest_unspent_income_date}.
                </div>
              )}
            </div>
            <div className="text-sm text-gray-300 space-y-1">
              <div>Balance: {money(foldin.safe.parts.account_balance_cents)}</div>
              <div>
                Bills before payday ({foldin.safe.next_payday_date}
                {foldin.safe.payday_estimated ? ", estimated" : ""}):{" "}
                −{money(foldin.safe.parts.upcoming_bills_before_payday_cents)}
              </div>
              <div>
                Promised to envelopes: −{money(foldin.safe.parts.envelope_obligations_cents)}
              </div>
              <div>
                Locked in vaults &amp; goals: −{money(foldin.safe.parts.vault_locks_cents)}
              </div>
              {foldin.safe.largest_envelope && (
                <div className="text-gray-500 text-xs">
                  Most room: {foldin.safe.largest_envelope.name} (
                  {money(foldin.safe.largest_envelope.remaining_cents)} left)
                </div>
              )}
            </div>
          </div>
        </Card>
      )}

      {suggestions && suggestions.length > 0 && (
        <Card className="mb-6 border border-amber-500/30" data-testid="household-suggestions">
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2">
            <Sparkles size={16} className="text-amber-300" /> Recurring bills we spotted
          </h3>
          <p className="text-xs text-gray-500 mt-1">
            From your transaction history. Nothing is added until you confirm it.
          </p>
          {suggestions.map((s) => (
            <div key={s.merchant_key + s.amount_cents} className="flex flex-wrap items-center justify-between gap-2 py-2 border-b border-white/5 last:border-0">
              <span className="text-sm text-gray-200">
                {s.suggested_name} — {money(s.amount_cents)} / {s.cadence}
                <span className="text-gray-500"> · seen {s.occurrences}× · next ~{s.suggested_next_due_date}</span>
              </span>
              <span className="flex gap-2">
                <Button size="sm" onClick={() => confirmSuggestion(s)}>Add bill</Button>
                <Button size="sm" variant="ghost" onClick={() => setSuggestions((p) => p.filter((x) => x !== s))}>
                  <X size={14} />
                </Button>
              </span>
            </div>
          ))}
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2 mb-2">
            <Wallet size={16} /> {t("household.accounts")}
          </h3>
          {summary.accounts.length === 0 && <Empty>No accounts yet — import a statement from Banking.</Empty>}
          {summary.accounts.map((a) => (
            <div key={a.bank_account_id} className="flex justify-between py-2 border-b border-white/5 last:border-0">
              <span className="text-sm text-gray-200">{a.name}</span>
              <span className="text-sm text-gray-300">
                {moneyFor(a.current_balance_cents, a.currency || base)}
              </span>
            </div>
          ))}
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2 mb-2">
            <PiggyBank size={16} /> {t("household.budgets")}
          </h3>
          {summary.budgets.rows.length === 0 && (
            <Empty>No budgets yet — add your first one below.</Empty>
          )}
          {summary.budgets.rows.map((r) => (
            <BudgetBar key={r.category} row={r} />
          ))}
          <div className="flex flex-wrap items-end gap-2 mt-3">
            <div className="flex-1 min-w-[120px]">
              <Label>Category</Label>
              <Input value={forms.budget.category} onChange={set("budget", "category")} placeholder="Groceries" />
            </div>
            <div className="w-28">
              <Label>Monthly $</Label>
              <Input type="number" value={forms.budget.amount} onChange={set("budget", "amount")} />
            </div>
            <label className="flex items-center gap-1 text-xs text-gray-400 pb-2">
              <input type="checkbox" checked={forms.budget.rollover} onChange={set("budget", "rollover")} /> Rollover
            </label>
            <Button size="sm" onClick={() => submit("budget")}><Plus size={14} /></Button>
          </div>
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 mb-2">{t("household.bills")}</h3>
          {summary.bills.length === 0 && (
            <Empty>No bills tracked yet — add one, or let us spot them from your transactions.</Empty>
          )}
          {summary.bills.map((b) => (
            <div key={b.id} className="flex items-center justify-between gap-2 py-2 border-b border-white/5 last:border-0">
              <span className="text-sm text-gray-200">
                {b.name} <span className="text-gray-500">· {money(b.amount_cents)} / {b.cadence}</span>
                {b.source === "detected" && <span className="ml-2 text-xs text-amber-300">auto-detected</span>}
              </span>
              <span className="flex items-center gap-2">
                <DueBadge bill={b} />
                <button className="text-gray-500 hover:text-red-300" onClick={() => remove(`/personal/recurring-bills/${b.id}`)} aria-label="Delete bill">
                  <Trash2 size={14} />
                </button>
              </span>
            </div>
          ))}
          <div className="flex flex-wrap items-end gap-2 mt-3">
            <div className="flex-1 min-w-[120px]">
              <Label>Name</Label>
              <Input value={forms.bill.name} onChange={set("bill", "name")} placeholder="Electric" />
            </div>
            <div className="w-24">
              <Label>$</Label>
              <Input type="number" value={forms.bill.amount} onChange={set("bill", "amount")} />
            </div>
            <div className="w-32">
              <Label>Next due</Label>
              <Input type="date" value={forms.bill.next_due_date} onChange={set("bill", "next_due_date")} />
            </div>
            <Button size="sm" onClick={() => submit("bill")}><Plus size={14} /></Button>
          </div>
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2 mb-2">
            <Target size={16} /> {t("household.goals")}
          </h3>
          {summary.goals.length === 0 && (
            <Empty>No savings goals yet — set one below and it can fund itself.</Empty>
          )}
          {summary.goals.map((g) => (
            <div key={g.id} className="py-2 border-b border-white/5 last:border-0">
              <div className="flex justify-between text-sm">
                <span className="text-gray-200">{g.name}</span>
                <span className="text-gray-400">{money(g.saved_cents)} of {money(g.target_cents)} · {g.progress_pct}%</span>
              </div>
              <div className="h-1.5 mt-1.5 overflow-hidden rounded-full bg-[#222838]">
                <div className="h-full bg-emerald-500" style={{ width: `${Math.min(g.progress_pct, 100)}%` }} />
              </div>
              {g.auto_fund && (
                <div className="text-xs text-gray-500 mt-1">
                  Self-funding: {money(g.funding_amount_cents)} / {g.funding_cadence}
                  {g.funding_destination_account_id &&
                    ` · moves to ${
                      summary.accounts.find(
                        (a) => a.bank_account_id === g.funding_destination_account_id,
                      )?.name || "savings account"
                    }`}
                  {g.last_funded_date && ` · last funded ${g.last_funded_date}`}
                </div>
              )}
            </div>
          ))}
          <div className="flex flex-wrap items-end gap-2 mt-3">
            <div className="flex-1 min-w-[120px]">
              <Label>Goal</Label>
              <Input value={forms.goal.name} onChange={set("goal", "name")} placeholder="Emergency fund" />
            </div>
            <div className="w-24">
              <Label>Target $</Label>
              <Input type="number" value={forms.goal.target} onChange={set("goal", "target")} />
            </div>
            <div className="w-24">
              <Label>Save $/mo</Label>
              <Input type="number" value={forms.goal.amount} onChange={set("goal", "amount")} />
            </div>
            <div className="w-32">
              <Label>From account</Label>
              <select className="w-full rounded bg-[#151a26] border border-white/10 px-2 py-2 text-sm" value={forms.goal.from_account} onChange={set("goal", "from_account")}>
                <option value="">—</option>
                {summary.accounts.map((a) => (
                  <option key={a.bank_account_id} value={a.bank_account_id}>{a.name}</option>
                ))}
              </select>
            </div>
            <div className="w-32">
              <Label>To account</Label>
              <select className="w-full rounded bg-[#151a26] border border-white/10 px-2 py-2 text-sm" value={forms.goal.to_account} onChange={set("goal", "to_account")}>
                <option value="">—</option>
                {summary.accounts.map((a) => (
                  <option key={a.bank_account_id} value={a.bank_account_id}>{a.name}</option>
                ))}
              </select>
            </div>
            <Button size="sm" onClick={() => submit("goal")}><Plus size={14} /></Button>
          </div>
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2 mb-2">
            <Mail size={16} /> {t("household.envelopes")}
          </h3>
          {(!foldin || foldin.envelopes.length === 0) && (
            <Empty>No envelopes yet — give a category a pot of its own below.</Empty>
          )}
          {(foldin?.envelopes || []).map((e) => (
            <EnvelopeBar key={e.id} row={e}
                         onDelete={() => removeFoldin(`/personal/envelopes/${e.id}`)} />
          ))}
          <div className="flex flex-wrap items-end gap-2 mt-3">
            <div className="flex-1 min-w-[110px]">
              <Label>Name</Label>
              <Input value={forms.envelope.name} onChange={set("envelope", "name")} placeholder="Groceries" />
            </div>
            <div className="flex-1 min-w-[110px]">
              <Label>Category</Label>
              <Input value={forms.envelope.category} onChange={set("envelope", "category")} placeholder="Groceries" />
            </div>
            <div className="w-24">
              <Label>$ / cycle</Label>
              <Input type="number" value={forms.envelope.amount} onChange={set("envelope", "amount")} />
            </div>
            <div className="w-20">
              <Label>Starts</Label>
              <Input type="number" min="1" max="28" value={forms.envelope.cycle_start_day} onChange={set("envelope", "cycle_start_day")} />
            </div>
            <label className="flex items-center gap-1 text-xs text-gray-400 pb-2">
              <input type="checkbox" checked={forms.envelope.rollover} onChange={set("envelope", "rollover")} /> Rollover
            </label>
            <Button size="sm" onClick={submitEnvelope}><Plus size={14} /></Button>
          </div>
          <div className="flex flex-wrap items-end gap-2 mt-3 border-t border-white/5 pt-3">
            <div className="w-32">
              <Label>Paycheck $</Label>
              <Input type="number" value={forms.paycheck.amount} onChange={set("paycheck", "amount")} />
            </div>
            <Button size="sm" variant="outline" onClick={suggestEnvelopes}>
              Suggest splits from my spending
            </Button>
          </div>
          {envSuggestions && envSuggestions.length > 0 && (
            <div className="mt-2">
              {envSuggestions.map((r) => (
                <div key={r.category} className="flex items-center justify-between py-1 text-sm text-gray-300">
                  <span>{r.category} — {money(r.suggested_cents)} <span className="text-gray-500">({r.share_pct}% of spending)</span></span>
                  <Button size="sm" variant="ghost" onClick={() => applySuggestion(r)}>Use</Button>
                </div>
              ))}
              <div className="text-xs text-gray-500">Suggestions only — nothing is saved until you add the envelope.</div>
            </div>
          )}
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2 mb-2">
            <Layers size={16} /> {t("household.savingsMethods")}
          </h3>
          {(!foldin || foldin.methods.length === 0) && (
            <Empty>Savings methods appear here.</Empty>
          )}
          {(foldin?.methods || []).map((m) => (
            <div key={m.key} className="py-2 border-b border-white/5 last:border-0">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-sm font-medium text-gray-200">{m.name}</span>
                {m.enrolled ? (
                  <span className="flex items-center gap-2">
                    <span className="text-xs text-gray-400">{money(m.saved_total_cents)} saved</span>
                    <button className="text-xs text-gray-500 hover:text-red-300" onClick={() => methodUnenroll(m)}>Stop</button>
                  </span>
                ) : (
                  <Button size="sm" variant="outline" onClick={() => methodEnroll(m)}>Start</Button>
                )}
              </div>
              <div className="text-xs text-gray-500 mt-0.5">{m.description}</div>
              {m.enrolled && (
                <>
                  {m.cycle_goal_cents > 0 && (
                    <div className="h-1.5 mt-1.5 overflow-hidden rounded-full bg-[#222838]">
                      <div className="h-full bg-emerald-500" style={{ width: `${Math.min(m.progress_pct, 100)}%` }} />
                    </div>
                  )}
                  <div className="flex items-center justify-between mt-1.5">
                    <span className="text-xs text-gray-500">
                      {m.yearly_pace && (
                        <>Yearly pace {money(m.yearly_pace.amount_cents)} · {m.yearly_pace.label} — {m.yearly_pace.basis}</>
                      )}
                    </span>
                    {m.next_action && (
                      <Button size="sm" onClick={() => methodAction(m)}>{m.next_action.label}</Button>
                    )}
                    {m.complete && <span className="text-xs text-emerald-300">Complete</span>}
                  </div>
                </>
              )}
            </div>
          ))}
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2 mb-2">
            <Lock size={16} /> {t("household.vaults")}
          </h3>
          {(!foldin || foldin.vaults.length === 0) && (
            <Empty>No vaults yet — park money behind a withdrawal timer below.</Empty>
          )}
          {(foldin?.vaults || []).map((v) => (
            <div key={v.id} className="py-2 border-b border-white/5 last:border-0">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm text-gray-200">
                  {v.name} <span className="text-gray-500">· {v.withdrawal_delay_hours}h timer</span>
                </span>
                <span className="flex items-center gap-2 text-sm text-gray-300">
                  {money(v.balance_cents)}
                  <button className="text-gray-500 hover:text-red-300" onClick={() => removeFoldin(`/personal/vaults/${v.id}`)} aria-label="Delete vault">
                    <Trash2 size={14} />
                  </button>
                </span>
              </div>
              {v.pending_withdrawal && (
                <div className="flex items-center justify-between mt-1">
                  <span className="text-xs text-amber-300">
                    {money(v.pending_withdrawal.amount_cents)} unlocks {v.pending_withdrawal.available_at.slice(0, 10)}
                  </span>
                  {v.pending_withdrawal.ready && (
                    <Button size="sm" onClick={() => vaultWithdraw(v)}>Withdraw now</Button>
                  )}
                </div>
              )}
            </div>
          ))}
          <div className="flex flex-wrap items-end gap-2 mt-3">
            <div className="flex-1 min-w-[110px]">
              <Label>New vault</Label>
              <Input value={forms.vault.name} onChange={set("vault", "name")} placeholder="Emergency stash" />
            </div>
            <div className="w-24">
              <Label>Start $</Label>
              <Input type="number" value={forms.vault.amount} onChange={set("vault", "amount")} />
            </div>
            <div className="w-20">
              <Label>Timer h</Label>
              <Input type="number" value={forms.vault.delay_hours} onChange={set("vault", "delay_hours")} />
            </div>
            <Button size="sm" onClick={submitVault}><Plus size={14} /></Button>
          </div>
          <div className="flex flex-wrap items-end gap-2 mt-2">
            <div className="w-40">
              <Label>Move money</Label>
              <select className="w-full rounded bg-[#151a26] border border-white/10 px-2 py-2 text-sm" value={forms.vaultMove.vault_id} onChange={set("vaultMove", "vault_id")}>
                <option value="">Pick a vault</option>
                {(foldin?.vaults || []).map((v) => (
                  <option key={v.id} value={v.id}>{v.name}</option>
                ))}
              </select>
            </div>
            <div className="w-24">
              <Label>$</Label>
              <Input type="number" value={forms.vaultMove.amount} onChange={set("vaultMove", "amount")} />
            </div>
            <Button size="sm" variant="outline" onClick={() => vaultMove("deposit")}>Deposit</Button>
            <Button size="sm" variant="outline" onClick={() => vaultMove("request-withdrawal")}>Start withdrawal</Button>
          </div>
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 mb-2">{t("household.assetsDebts")}</h3>
          {(nw.manual_items || []).length === 0 && (
            <Empty>No assets or debts added yet — track a house, a car, or a loan below.</Empty>
          )}
          {(nw.manual_items || []).map((a) => (
            <div key={a.id} className="flex items-center justify-between gap-2 py-2 border-b border-white/5 last:border-0">
              <span className="text-sm text-gray-200">
                {a.name} <span className="text-gray-500">· {a.kind.replace("_", " ")}</span>
              </span>
              <span className="flex items-center gap-2 text-sm text-gray-300">
                {moneyFor(a.value_cents, a.currency || base)}
                {a.currency && a.currency !== base && (
                  <span className="text-gray-500">≈ {moneyFor(a.converted_cents, base)}{a.fx_applied ? "" : " (no rate)"}</span>
                )}
                {a.kind === "loan" && a.term_months && (
                  <Button size="sm" variant="outline" onClick={() => showSchedule(a)}>Schedule</Button>
                )}
                <button className="text-gray-500 hover:text-red-300" onClick={() => remove(`/personal/assets/${a.id}`)} aria-label="Delete asset">
                  <Trash2 size={14} />
                </button>
              </span>
            </div>
          ))}
          {schedule && (
            <div className="mt-3 max-h-56 overflow-auto rounded bg-black/20 p-3" data-testid="household-schedule">
              <div className="text-sm text-gray-200 mb-2">
                {schedule.name}: {money(schedule.payment_cents)}/mo · payoff {schedule.payoff_date} · interest {money(schedule.total_interest_cents)}
              </div>
              <table className="w-full text-xs text-gray-400">
                <thead><tr className="text-left"><th>#</th><th>Date</th><th>Payment</th><th>Principal</th><th>Interest</th><th>Balance</th></tr></thead>
                <tbody>
                  {schedule.rows.map((r) => (
                    <tr key={r.month}><td>{r.month}</td><td>{r.date}</td><td>{money(r.payment_cents)}</td><td>{money(r.principal_cents)}</td><td>{money(r.interest_cents)}</td><td>{money(r.balance_cents)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="flex flex-wrap items-end gap-2 mt-3">
            <div className="flex-1 min-w-[120px]">
              <Label>Name</Label>
              <Input value={forms.asset.name} onChange={set("asset", "name")} placeholder="House, car loan…" />
            </div>
            <div className="w-32">
              <Label>Type</Label>
              <select className="w-full rounded bg-[#151a26] border border-white/10 px-2 py-2 text-sm" value={forms.asset.kind} onChange={set("asset", "kind")}>
                <option value="property">Property</option>
                <option value="vehicle">Vehicle</option>
                <option value="investment">Investment</option>
                <option value="other_asset">Other asset</option>
                <option value="loan">Loan</option>
                <option value="other_debt">Other debt</option>
              </select>
            </div>
            <div className="w-24">
              <Label>Value $</Label>
              <Input type="number" value={forms.asset.value} onChange={set("asset", "value")} />
            </div>
            <div className="w-20">
              <Label>Curr.</Label>
              <Input value={forms.asset.currency} onChange={set("asset", "currency")} placeholder={base} />
            </div>
            <Button size="sm" onClick={() => submit("asset")}><Plus size={14} /></Button>
          </div>
        </Card>

        <Card>
          <h3 className="font-display font-semibold text-gray-100 flex items-center gap-2 mb-2">
            <Sparkles size={16} className="text-amber-300" /> {t("household.insights")}
          </h3>
          {summary.insights.length === 0 && <Empty>Insights appear as your transactions come in.</Empty>}
          {summary.insights.map((c) => (
            <div key={c.kind + c.title} className="py-2 border-b border-white/5 last:border-0">
              <div className={`text-sm font-medium ${c.tone === "warn" ? "text-amber-300" : c.tone === "good" ? "text-emerald-300" : "text-gray-200"}`}>
                {c.title}
              </div>
              <div className="text-sm text-gray-400">{c.detail}</div>
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}
