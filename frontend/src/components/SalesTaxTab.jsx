import { useEffect, useState } from "react";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { toast } from "sonner";
import { MapPin, Plus, Power } from "lucide-react";
import { useTranslation } from "react-i18next";
const MONTHS = ["All year", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Sales-tax jurisdictions + per-period liability ("what you owe, and where"). */
export default function SalesTaxTab({
  canWrite
}) {
  const {
    t
  } = useTranslation();
  const curYear = new Date().getFullYear();
  const [juris, setJuris] = useState([]);
  const [year, setYear] = useState(curYear);
  const [month, setMonth] = useState(0);
  const [liab, setLiab] = useState(null);
  const [form, setForm] = useState({
    name: "",
    state: "",
    rate_pct: ""
  });
  const [busy, setBusy] = useState(false);
  const loadJuris = () => api.get("/sales-tax/jurisdictions").then(setJuris).catch(() => setJuris([]));
  const loadLiab = () => {
    const q = month ? `?year=${year}&month=${month}` : `?year=${year}`;
    api.get(`/sales-tax/liability${q}`).then(setLiab).catch(() => setLiab(null));
  };
  useEffect(() => {
    loadJuris();
  }, []);
  useEffect(() => {
    loadLiab();
  }, [year, month]);
  const addJuris = async () => {
    if (!form.name.trim() || !form.state.trim() || !form.rate_pct) {
      toast.error(t("salestaxtab.nameStateAndRateAreRequired"));
      return;
    }
    setBusy(true);
    try {
      await api.post("/sales-tax/jurisdictions", {
        name: form.name.trim(),
        state: form.state.trim(),
        rate_pct: parseFloat(form.rate_pct)
      });
      toast.success(t("salestaxtab.jurisdictionAdded"));
      setForm({
        name: "",
        state: "",
        rate_pct: ""
      });
      loadJuris();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const toggle = async j => {
    try {
      await api.patch(`/sales-tax/jurisdictions/${j.id}`, {
        active: !j.active
      });
      toast.success(j.active ? "Jurisdiction deactivated" : "Jurisdiction reactivated");
      loadJuris();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const rows = liab?.jurisdictions || [];
  const unassigned = rows.find(r => r.jurisdiction_id === "unassigned");
  return <div className="space-y-4">
      <Card>
        <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
          <MapPin className="w-4 h-4" /> {t("salestaxtab.taxJurisdictions")}</h3>
        <p className="text-sm text-gray-500 mb-4">{t("salestaxtab.everywhereYouCollectSalesTaxState")}</p>
        {canWrite && <div className="flex flex-col sm:flex-row gap-2 mb-4">
            <Input value={form.name} onChange={e => setForm({
          ...form,
          name: e.target.value
        })} placeholder={t("salestaxtab.eGTravisCountyTX")} className="bg-[#14171E] border-[#222838] flex-1" data-testid="st-name" />
            <Input value={form.state} onChange={e => setForm({
          ...form,
          state: e.target.value.toUpperCase().slice(0, 2)
        })} placeholder={t("salestaxtab.st")} className="bg-[#14171E] border-[#222838] w-20" data-testid="st-state" />
            <Input value={form.rate_pct} onChange={e => setForm({
          ...form,
          rate_pct: e.target.value
        })} placeholder={t("salestaxtab.rate")} type="number" step="0.01" className="bg-[#14171E] border-[#222838] w-28 font-mono" data-testid="st-rate" />
            <Button onClick={addJuris} disabled={busy} data-testid="st-add" className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">
              <Plus size={15} className="mr-1" /> {t("salestaxtab.add")}</Button>
          </div>}
        {juris.length > 0 ? <ul className="space-y-2">
            {juris.map(j => <li key={j.id} className={`flex items-center justify-between rounded-lg border px-3 py-2 ${j.active ? "bg-[#14171E] border-[#222838]" : "bg-[#0F1218] border-[#1b2230] opacity-60"}`}>
                <div>
                  <div className="text-sm text-gray-100">{j.name}</div>
                  <div className="text-[11px] text-gray-500">{j.state} · {j.rate_pct}%{j.notes ? ` · ${j.notes}` : ""}</div>
                </div>
                {canWrite && <button onClick={() => toggle(j)} title={j.active ? "Deactivate" : "Reactivate"} className="text-gray-500 hover:text-amber-400 transition-colors" data-testid={`st-toggle-${j.id}`}>
                    <Power size={15} />
                  </button>}
              </li>)}
          </ul> : <p className="text-sm text-gray-500">{t("salestaxtab.noJurisdictionsYetAddTheFirst")}</p>}
      </Card>

      <Card className="p-0 overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 px-5 pt-4 pb-2">
          <h3 className="font-semibold text-gray-100 mr-auto">{t("salestaxtab.liabilityReport")}</h3>
          <Select value={String(year)} onValueChange={v => setYear(parseInt(v, 10))}>
            <SelectTrigger className="bg-[#14171E] border-[#222838] w-24" data-testid="st-year"><SelectValue /></SelectTrigger>
            <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
              {[curYear, curYear - 1, curYear - 2].map(y => <SelectItem key={y} value={String(y)}>{y}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={String(month)} onValueChange={v => setMonth(parseInt(v, 10))}>
            <SelectTrigger className="bg-[#14171E] border-[#222838] w-28" data-testid="st-month"><SelectValue /></SelectTrigger>
            <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
              {MONTHS.map((m, i) => <SelectItem key={m} value={String(i)}>{m}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        {unassigned && unassigned.tax_collected_cents > 0 && <div className="mx-5 mb-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200" data-testid="st-unassigned-note">
            {money(unassigned.tax_collected_cents)} {t("salestaxtab.ofSalesTaxIsnTTagged")}</div>}
        <table className="w-full text-sm" data-testid="table-st-liability">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">{t("salestaxtab.jurisdiction")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("salestaxtab.rate2")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("salestaxtab.invoices")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("salestaxtab.taxableSales")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("salestaxtab.taxCollected")}</th>
          </tr></thead>
          <tbody>
            {rows.map(r => <tr key={r.jurisdiction_id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-5 py-3 text-gray-100">{r.name}{r.state ? <span className="text-gray-500 text-xs ml-2">{r.state}</span> : null}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-400">{r.rate_pct != null ? `${r.rate_pct}%` : "—"}</td>
                <td className="px-5 py-3 text-right text-gray-400">{r.invoice_count}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-300">{money(r.taxable_sales_cents)}</td>
                <td className="px-5 py-3 text-right font-mono text-amber-400">{money(r.tax_collected_cents)}</td>
              </tr>)}
            {rows.length === 0 && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">{t("salestaxtab.noTaxedInvoicesInThisPeriod")}</td></tr>}
          </tbody>
          {liab && rows.length > 0 && <tfoot><tr className="border-t border-[#222838] bg-[#0F1218]">
              <td className="px-5 py-3 text-gray-200 font-semibold">{t("salestaxtab.total")}</td>
              <td />
              <td className="px-5 py-3 text-right text-gray-300">{liab.totals.invoice_count}</td>
              <td className="px-5 py-3 text-right font-mono text-gray-200">{money(liab.totals.taxable_sales_cents)}</td>
              <td className="px-5 py-3 text-right font-mono text-amber-400 font-semibold">{money(liab.totals.tax_collected_cents)}</td>
            </tr></tfoot>}
        </table>
      </Card>
    </div>;
}
