import { useState } from "react";
import { Card } from "@/components/Page";
import { api, errMsg } from "@/lib/api";
import { today } from "@/lib/format";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Landmark } from "lucide-react";

/** Owner-only: one-time opening-balances migration. Posts a balanced
 *  opening entry (equity plugs the difference). The backend rejects
 *  a second run, so this card is safe to leave in place. */
export default function OpeningBalances() {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [f, setF] = useState({ cash_register: "", cash_bank: "", accounts_payable: "", as_of: today() });

  if (user?.role !== "owner") return null;

  const run = async () => {
    setBusy(true);
    try {
      const r = await api.post("/ledger/opening-balances", {
        cash_register: parseFloat(f.cash_register) || 0,
        cash_bank: parseFloat(f.cash_bank) || 0,
        accounts_payable: parseFloat(f.accounts_payable) || 0,
        as_of: f.as_of || null,
      });
      toast.success(`Opening balances posted — entry ${r.entry_number}`);
      setDone(true); setOpen(false);
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  return (
    <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <Landmark className="w-4 h-4" /> Opening balances
      </h3>
      <p className="text-sm text-gray-500 mb-4">
        {done
          ? "Opening balances have been posted. The books now start from these figures."
          : "Bringing balances over from another system? Enter starting figures once — inventory and receivables are pulled in automatically. Owner's equity plugs the difference."}
      </p>
      {!done && !open && (
        <Button variant="outline" onClick={() => setOpen(true)} data-testid="opening-open">Enter opening balances</Button>
      )}
      {!done && open && (
        <div className="space-y-3 max-w-md" data-testid="opening-form">
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">Cash — register $</Label><Input type="number" step="0.01" value={f.cash_register} onChange={(e) => setF({ ...f, cash_register: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" data-testid="opening-cash-register" /></div>
            <div><Label className="text-xs">Cash — bank $</Label><Input type="number" step="0.01" value={f.cash_bank} onChange={(e) => setF({ ...f, cash_bank: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" data-testid="opening-cash-bank" /></div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">Accounts payable $</Label><Input type="number" step="0.01" value={f.accounts_payable} onChange={(e) => setF({ ...f, accounts_payable: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" data-testid="opening-ap" /></div>
            <div><Label className="text-xs">As of</Label><Input type="date" value={f.as_of} onChange={(e) => setF({ ...f, as_of: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <div className="flex gap-2">
            <Button onClick={run} disabled={busy} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="opening-run">
              {busy ? "Posting…" : "Post opening balances"}
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
          </div>
          <p className="text-[11px] text-gray-600">This can only run once. Double-check the figures — reversing means a manual journal entry.</p>
        </div>
      )}
    </Card>
  );
}
