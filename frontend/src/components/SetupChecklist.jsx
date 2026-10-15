import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Card } from "@/components/Page";
import { CheckCircle2, Circle, Scissors } from "lucide-react";

/** Service-shop edition onboarding checklist. Renders nothing when complete
 *  or when the backend data can't be loaded. Dependency-free (no query lib). */
export default function SetupChecklist() {
  const [data, setData] = useState(null);
  useEffect(() => { api.get("/edition/checklist").then(setData).catch(() => setData(null)); }, []);
  if (!data || data.complete || (data.items || []).length === 0) return null;
  return (
    <Card className="p-5 mb-4" data-testid="setup-checklist">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 font-medium text-gray-100">
          <Scissors size={16} className="text-amber-400" /> Get your {data.shop_label || "shop"} set up
        </div>
        <span className="text-xs text-gray-500">{data.done}/{data.total} done</span>
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        {data.items.map((s) => (
          <Link key={s.id} to={s.link} className="flex items-start gap-2.5 rounded-lg border border-[#222838] p-3 hover:bg-[#161b25]" data-testid={`checklist-${s.id}`}>
            {s.done
              ? <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-400" />
              : <Circle size={16} className="mt-0.5 shrink-0 text-gray-600" />}
            <span>
              <span className={`text-sm font-medium ${s.done ? "line-through text-gray-500" : "text-gray-200"}`}>{s.label}</span>
              <span className="block text-xs text-gray-500">{s.hint}</span>
            </span>
          </Link>
        ))}
      </div>
    </Card>
  );
}
