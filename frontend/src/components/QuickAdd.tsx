import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, ArrowDownLeft, ArrowUpRight } from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";
import { businessToday, errMsg } from "@/lib/format";
import { queryClient } from "@/lib/queryClient";
import type { PayMethod } from "@/lib/types";
import { Field, NativeSelect } from "@/components/Common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

const METHODS: PayMethod[] = ["cash", "card", "bank", "other"];

// Quick, plain-language entry for owners: "Money in" posts income, "Money out" posts an expense.
// Both book straight to the double-entry journal behind the scenes.
export default function QuickAdd() {
  const [open, setOpen] = useState(false);
  const [dir, setDir] = useState<"in" | "out">("out");
  const blank = { amount: 0, category: "", date: businessToday(), payment_method: "cash" as PayMethod, description: "" };
  const [f, setF] = useState(blank);

  const save = useMutation({
    mutationFn: async () => {
      if (dir === "out") return apiPost("/expenses", { ...f, category: f.category || "Other", vendor_id: null, receipt_url: null, receipt_name: "" });
      return apiPost("/transactions", { kind: "income", category: f.category || "Other income", amount: f.amount, date: f.date, description: f.description, barber_id: null, payment_method: f.payment_method });
    },
    onSuccess: () => {
      toast.success(dir === "out" ? "Money out recorded" : "Money in recorded");
      for (const k of ["dashboard", "expenses", "books-overview", "transactions"]) queryClient.invalidateQueries({ queryKey: [k] });
      setOpen(false); setF(blank);
    },
    onError: (e) => toast.error(errMsg(e)),
  });
  void apiGet; // categories are free text here for speed

  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)} data-testid="quick-add-button"><Plus /> Quick add</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-sm" data-testid="quick-add-dialog">
          <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
            <DialogHeader><DialogTitle>Quick add</DialogTitle></DialogHeader>
            <div className="grid grid-cols-2 gap-2">
              <Button type="button" variant={dir === "in" ? "default" : "outline"} onClick={() => setDir("in")} data-testid="quick-money-in"><ArrowDownLeft className="size-4" /> Money in</Button>
              <Button type="button" variant={dir === "out" ? "default" : "outline"} onClick={() => setDir("out")} data-testid="quick-money-out"><ArrowUpRight className="size-4" /> Money out</Button>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Amount"><Input type="number" step="0.01" min={0.01} required value={f.amount || ""} onChange={(e) => setF({ ...f, amount: Number(e.target.value) })} className="font-mono" data-testid="quick-amount" /></Field>
              <Field label="Date"><Input type="date" value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} data-testid="quick-date" /></Field>
              <Field label={dir === "out" ? "What for?" : "From what?"} className="col-span-2"><Input placeholder={dir === "out" ? "Supplies, Rent…" : "Walk-in haircut…"} value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })} data-testid="quick-category" /></Field>
              <Field label="Method" className="col-span-2"><NativeSelect value={f.payment_method} onChange={(e) => setF({ ...f, payment_method: e.target.value as PayMethod })} data-testid="quick-method">{METHODS.map((m) => <option key={m} value={m}>{m}</option>)}</NativeSelect></Field>
            </div>
            <DialogFooter><Button type="submit" disabled={save.isPending} data-testid="quick-save">Save</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
