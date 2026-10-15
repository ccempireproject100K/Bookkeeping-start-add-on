import { useEffect, useState } from "react";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { ShieldAlert, Check } from "lucide-react";

/** Security alerts feed: new-device logins, sentinel findings, passkey events. */
import { useTranslation } from "react-i18next";
export default function SecurityAlerts() {
  const {
    t
  } = useTranslation();
  const [alerts, setAlerts] = useState([]);
  const [unacked, setUnacked] = useState(0);
  const load = async () => {
    try {
      const r = await api.get("/alerts", {
        params: {
          limit: 20
        }
      });
      setAlerts(r.alerts || []);
      setUnacked(r.unacked || 0);
    } catch {/* alerts unavailable */}
  };
  useEffect(() => {
    load();
  }, []);
  const ack = async id => {
    try {
      await api.post(`/alerts/${id}/ack`, {});
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!alerts.length) return null;
  return <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <ShieldAlert className="w-4 h-4 text-amber-400" /> {t("securityalerts.securityAlerts")}{unacked > 0 && <span className="text-[11px] bg-amber-500/15 text-amber-300 rounded-full px-2 py-0.5">
            {unacked} {t("securityalerts.new")}</span>}
      </h3>
      <p className="text-sm text-gray-500 mb-3">{t("securityalerts.unusualSignInsAndAnythingThe")}</p>
      <ul className="space-y-2">
        {alerts.slice(0, 8).map(a => <li key={a.id} className={`flex items-start justify-between gap-3 rounded-lg border px-3 py-2 ${a.acknowledged_at ? "bg-[#14171E] border-[#222838] opacity-60" : "bg-amber-500/5 border-amber-500/25"}`}>
            <div>
              <div className="text-sm text-gray-100">{a.title}</div>
              {a.detail && <div className="text-xs text-gray-500 mt-0.5">{a.detail}</div>}
              <div className="text-[11px] text-gray-600 mt-1">
                {a.created_at ? new Date(a.created_at).toLocaleString() : ""}
              </div>
            </div>
            {!a.acknowledged_at && <Button variant="ghost" size="sm" onClick={() => ack(a.id)} data-testid={`alert-ack-${a.id}`} title={t("securityalerts.markReviewed")}>
                <Check size={15} />
              </Button>}
          </li>)}
      </ul>
    </Card>;
}
