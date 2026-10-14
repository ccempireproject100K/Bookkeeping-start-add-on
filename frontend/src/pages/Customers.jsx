import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Plus, MapPin, Phone, Mail } from "lucide-react";

export default function Customers() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [rows, setRows] = useState(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", email: "", phone: "", address: "" });

  const load = () => api.get("/customers").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []);

  const create = async () => {
    try {
      await api.post("/customers", {
        name: form.name, email: form.email || null, phone: form.phone || null,
        locations: form.address ? [{ label: "Primary", address: form.address }] : [],
      });
      toast.success("Customer added");
      setOpen(false); setForm({ name: "", email: "", phone: "", address: "" }); load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!rows) return <Loading />;

  return (
    <div className="animate-rise">
      <PageHeader title="Customers" subtitle={`${rows.length} customers & service locations`} testid="customers-page">
        {isOwner && <Button data-testid="btn-add-customer" onClick={() => setOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />Add Customer</Button>}
      </PageHeader>

      <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {rows.map((c) => (
          <Card key={c.id} data-testid={`customer-${c.id}`} className="hover:border-[#34404f] transition-colors">
            <div className="font-display font-semibold text-lg">{c.name}</div>
            <div className="mt-3 space-y-1.5 text-sm text-gray-400">
              {c.locations?.map((l, i) => <div key={i} className="flex items-start gap-2"><MapPin size={14} className="mt-0.5 text-amber-400" /><span>{l.address}</span></div>)}
              {c.phone && <div className="flex items-center gap-2"><Phone size={14} className="text-gray-500" />{c.phone}</div>}
              {c.email && <div className="flex items-center gap-2"><Mail size={14} className="text-gray-500" />{c.email}</div>}
            </div>
          </Card>
        ))}
        {rows.length === 0 && <Card><div className="text-gray-500 text-sm text-center py-6">No customers yet</div></Card>}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>Add Customer</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label className="text-xs">Name</Label><Input data-testid="customer-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Service Address</Label><Input data-testid="customer-address" value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="grid grid-cols-2 gap-3">
              <div><Label className="text-xs">Phone</Label><Input data-testid="customer-phone" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
              <div><Label className="text-xs">Email</Label><Input data-testid="customer-email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            </div>
          </div>
          <DialogFooter><Button data-testid="customer-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">Add</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
