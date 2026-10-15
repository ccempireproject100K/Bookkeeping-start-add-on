import { useEffect, useState } from "react";
import { Card } from "@/components/Page";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { useNiche } from "@/context/NicheContext";
import { useAuth } from "@/context/AuthContext";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { LayoutTemplate } from "lucide-react";

/** Owner-only: swap the workspace's niche face (branding + terminology).
 *  Display-only — the chart of accounts seeded at creation is untouched. */
import { useTranslation } from "react-i18next";
export default function NicheSwitcher() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const {
    pack,
    refresh
  } = useNiche();
  const [niches, setNiches] = useState([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (user?.role === "owner") api.get("/niches").then(setNiches).catch(() => setNiches([]));
  }, [user?.role]);
  if (user?.role !== "owner") return null;
  const switchNiche = async nid => {
    if (nid === pack?.id) return;
    setBusy(true);
    try {
      const r = await api.patch("/edition/niche", {
        niche_id: nid
      });
      await refresh();
      toast.success(`Now showing the ${r.product_name} face`);
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <LayoutTemplate className="w-4 h-4" /> {t("nicheswitcher.nicheFace")}</h3>
      <p className="text-sm text-gray-500 mb-4">{t("nicheswitcher.swapTheSiteSTradeFace")}</p>
      <Select value={pack?.id || ""} onValueChange={switchNiche} disabled={busy || !niches.length}>
        <SelectTrigger className="bg-[#14171E] border-[#222838] max-w-sm" data-testid="niche-select">
          <SelectValue placeholder={t("nicheswitcher.chooseATrade")} />
        </SelectTrigger>
        <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
          {niches.map(n => <SelectItem key={n.id} value={n.id} data-testid={`niche-option-${n.id}`}>
              {n.product_name} — {n.tagline}
            </SelectItem>)}
        </SelectContent>
      </Select>
    </Card>;
}
