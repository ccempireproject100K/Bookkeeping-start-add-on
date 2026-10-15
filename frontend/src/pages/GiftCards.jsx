import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, dollars, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Gift, Plus } from "lucide-react";

export default function GiftCards() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [out, setOut] = useState(null);
  const [cards, setCards] = useState([]);
  const [issueOpen, setIssueOpen] = useState(false);
  const [redeemCard, setRedeemCard] = useState(null);
  const [form, setForm] = useState({ amount: "", recipient: "", payment_method: "card" });
  const [redeemAmt, setRedeemAmt] = useState("");

  const load = () => {
    api.get("/gift-cards/outstanding").then(setOut);
    api.get("/gift-cards").then(setCards).catch(() => setCards([]));
  };
  useEffect(() => { load(); }, []);

  const issue = async () => {
    try {
      await api.post("/gift-cards", { amount: parseFloat(form.amount) || 0, recipient: form.recipient, payment_method: form.payment_method });
      toast.success("Gift card issued — liability booked to 2200");
      setIssueOpen(false); setForm({ amount: "", recipient: "", payment_method: "card" }); load();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const redeem = async () => {
    try {
      await api.post(`/gift-cards/${redeemCard.code}/redeem`, { amount: parseFloat(redeemAmt) || 0, revenue_code: "4020" });
      toast.success("Redeemed — revenue recognized");
      setRedeemCard(null); setRedeemAmt(""); load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!out) return <Loading />;

  return (
    <div className="animate-rise">
      <PageHeader title="Gift Cards" subtitle="Prepaid packages as deferred revenue (2200 Unearned Revenue)" testid="gift-cards-page">
        {isOwner && <Button data-testid="btn-issue-gift-card" onClick={() => setIssueOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />Issue Card</Button>}
      </PageHeader>

      <div className="grid sm:grid-cols-3 gap-4 mb-6">
        <Card className="bg-gradient-to-br from-amber-500/10 to-transparent border-amber-500/30">
          <div className="flex items-center justify-between"><span className="text-xs uppercase tracking-wider text-amber-500/90">Outstanding Liability</span><Gift size={18} className="text-amber-400" /></div>
          <div className="mt-3 font-mono text-2xl font-semibold text-amber-400" data-testid="gift-outstanding-total">{dollars(out.total)}</div>
          <div className="text-[11px] text-gray-500 mt-1">{out.count} active cards</div>
        </Card>
      </div>

      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-gift-cards">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">Code</th>
            <th className="px-5 py-3 text-left font-medium">Recipient</th>
            <th className="px-5 py-3 text-left font-medium">Issued</th>
            <th className="px-5 py-3 text-right font-medium">Initial</th>
            <th className="px-5 py-3 text-right font-medium">Balance</th>
            <th className="px-5 py-3 text-left font-medium">Status</th>
            {isOwner && <th className="px-5 py-3 text-right font-medium">Actions</th>}
          </tr></thead>
          <tbody>
            {cards.map((c) => (
              <tr key={c.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-5 py-3 font-mono text-amber-400">{c.code}</td>
                <td className="px-5 py-3 text-gray-200">{c.recipient}</td>
                <td className="px-5 py-3 text-gray-400">{fmtDate(c.created_at?.slice(0, 10))}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-400">{money(c.initial_cents)}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-100">{money(c.balance_cents)}</td>
                <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${c.status === "active" ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20" : "bg-gray-500/10 text-gray-400 border-gray-500/20"}`}>{c.status}</span></td>
                {isOwner && <td className="px-5 py-3 text-right">{c.status === "active" && <Button size="sm" className="h-7 bg-amber-600 hover:bg-amber-500 text-black" onClick={() => { setRedeemCard(c); setRedeemAmt(""); }} data-testid={`redeem-${c.code}`}>Redeem</Button>}</td>}
              </tr>
            ))}
            {cards.length === 0 && <tr><td colSpan={isOwner ? 7 : 6} className="px-5 py-12 text-center text-gray-500">No gift cards</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={issueOpen} onOpenChange={setIssueOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>Issue Gift Card</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label className="text-xs">Recipient</Label><Input data-testid="gift-recipient" value={form.recipient} onChange={(e) => setForm({ ...form, recipient: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Amount ($)</Label><Input data-testid="gift-amount" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <DialogFooter><Button data-testid="gift-save" onClick={issue} className="bg-amber-600 hover:bg-amber-500 text-black">Issue</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!redeemCard} onOpenChange={(o) => !o && setRedeemCard(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>Redeem {redeemCard?.code}</DialogTitle></DialogHeader>
          <div className="text-sm text-gray-400 mb-2">Balance: <span className="font-mono text-gray-100">{money(redeemCard?.balance_cents || 0)}</span></div>
          <div><Label className="text-xs">Redeem Amount ($)</Label><Input data-testid="redeem-amount" value={redeemAmt} onChange={(e) => setRedeemAmt(e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <DialogFooter><Button data-testid="redeem-save" onClick={redeem} className="bg-emerald-600 hover:bg-emerald-500 text-black">Redeem</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
