import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useNiche } from "@/context/NicheContext";
import { toast } from "sonner";

/** Firm branding for white-label: the firm's face on the client's books. */
import { useTranslation } from "react-i18next";
export default function BrandingCard() {
  const {
    t
  } = useTranslation();
  const {
    refresh
  } = useNiche();
  const [b, setB] = useState({
    firm_name: "",
    logo_url: "",
    accent_color: "",
    support_email: "",
    tagline: ""
  });
  useEffect(() => {
    api.get("/whitelabel/branding").then(r => setB({
      firm_name: r.firm_name || "",
      logo_url: r.logo_url || "",
      accent_color: r.accent_color || "",
      support_email: r.support_email || "",
      tagline: r.tagline || ""
    })).catch(() => {});
  }, []);
  const save = async () => {
    try {
      const upd = Object.fromEntries(Object.entries(b).filter(([_, v]) => v.trim()));
      await api.patch("/whitelabel/branding", upd);
      toast.success(t("brandingcard.brandingSavedYourFirmSFace"));
      refresh();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  return <Card>
      <div className="font-medium mb-1">{t("brandingcard.firmBrandingWhiteLabel")}</div>
      <p className="text-xs text-gray-500 mb-3">{t("brandingcard.clientsSeeYourFirmSName")}</p>
      <div className="grid grid-cols-2 gap-3">
        <div><Label className="text-xs">{t("brandingcard.firmName")}</Label><Input value={b.firm_name} onChange={e => setB({
          ...b,
          firm_name: e.target.value
        })} placeholder={t("brandingcard.acmeAccounting")} className="bg-[#0F1218] border-[#222838]" /></div>
        <div><Label className="text-xs">{t("brandingcard.tagline")}</Label><Input value={b.tagline} onChange={e => setB({
          ...b,
          tagline: e.target.value
        })} placeholder={t("brandingcard.booksYouLlNeverWorryAbout")} className="bg-[#0F1218] border-[#222838]" /></div>
        <div><Label className="text-xs">{t("brandingcard.logoURL")}</Label><Input value={b.logo_url} onChange={e => setB({
          ...b,
          logo_url: e.target.value
        })} placeholder="https://…" className="bg-[#0F1218] border-[#222838]" /></div>
        <div><Label className="text-xs">{t("brandingcard.accentColor")}</Label><Input value={b.accent_color} onChange={e => setB({
          ...b,
          accent_color: e.target.value
        })} placeholder="#f59e0b" className="bg-[#0F1218] border-[#222838] font-mono" /></div>
        <div className="col-span-2"><Label className="text-xs">{t("brandingcard.supportEmail")}</Label><Input value={b.support_email} onChange={e => setB({
          ...b,
          support_email: e.target.value
        })} placeholder={t("brandingcard.helpAcmeaccountingCom")} className="bg-[#0F1218] border-[#222838]" /></div>
      </div>
      <Button size="sm" className="mt-3" onClick={save} data-testid="branding-save">{t("brandingcard.saveBranding")}</Button>
    </Card>;
}
