import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { toast } from "sonner";
import { Copy } from "lucide-react";
import { fmtDate } from "@/lib/format";

// Operator console for referral PLATFORM credentials (the mint behind
// POST /api/referrals/admin/platform-credentials). Each product
// surface (AI Office now; Academy and hub later) authenticates to this
// host with its own platform credential, scope referrals:sync. The
// backend stores only a SHA-256 hash; the full credential value comes
// back exactly once — in the mint/rotate response — and this page
// holds it only in component state inside the reveal dialog: never in
// localStorage, never in the URL, never logged, and gone the moment
// the dialog closes or the page unmounts (navigation).
//
// Gating mirrors the ReferralPayouts operator page: the backend
// requires ledger:admin, which maps to the workspace owner role, and
// the UI shows the console only to owners.
const INPUT_CLS = "bg-[#0F1218] border-[#222838]";
// Mirrors PLATFORMS in backend/lib/integration_auth.py.
const PLATFORMS = ["trade_circle", "books", "momo"];
const MAX_EXPIRES_DAYS = 730; // mirrors MAX_EXPIRES_DAYS backend-side

function CopyRow({ value, copyLabel, copiedLabel }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="flex-1 break-all rounded bg-black/40 px-2 py-1 text-xs">
        {value}
      </code>
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          navigator.clipboard.writeText(value);
          setCopied(true);
        }}
      >
        <Copy className="h-3 w-3" /> {copied ? copiedLabel : copyLabel}
      </Button>
    </div>
  );
}

export default function PlatformCredentials() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [platform, setPlatform] = useState("momo");
  const [label, setLabel] = useState("");
  const [expiresDays, setExpiresDays] = useState("365");
  // The once-only reveal: { value, rotated } or null. Component state
  // ONLY — closing the dialog or leaving the page destroys it.
  const [reveal, setReveal] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.get("/referrals/admin/platform-credentials");
      setRows(data || []);
    } catch (e) {
      toast.error(errMsg(e));
    }
    setLoading(false);
  };
  useEffect(() => { if (isOwner) load(); }, [isOwner]);

  const act = async (fn, okMsg) => {
    setBusy(true);
    try {
      await fn();
      if (okMsg) toast.success(okMsg);
      await load();
    } catch (e) {
      toast.error(errMsg(e));
    }
    setBusy(false);
  };

  const mint = () =>
    act(async () => {
      const days = parseInt(expiresDays, 10);
      const res = await api.post("/referrals/admin/platform-credentials", {
        platform,
        label: label.trim(),
        expires_in_days:
          Number.isFinite(days) && days >= 1 && days <= MAX_EXPIRES_DAYS
            ? days
            : undefined,
      });
      setLabel("");
      // Show the secret first; the list reload (in act) happens behind
      // the dialog and never touches the reveal value.
      setReveal({ value: res.credential, rotated: false });
    }, t("platformCredentials.mintedToast"));

  const rotate = (row) => {
    if (!window.confirm(t("platformCredentials.rotateConfirm"))) return;
    act(async () => {
      const res = await api.post(
        `/referrals/admin/platform-credentials/${row.id}/rotate`, {});
      setReveal({ value: res.credential, rotated: true });
    }, t("platformCredentials.rotatedToast"));
  };

  const revoke = (row) => {
    if (!window.confirm(t("platformCredentials.revokeConfirm"))) return;
    act(async () => {
      await api.post(
        `/referrals/admin/platform-credentials/${row.id}/revoke`, {});
    }, t("platformCredentials.revokedToast"));
  };

  const statusOf = (row) => {
    if (row.revoked_at) {
      return {
        key: "revoked",
        label: t("platformCredentials.statusRevoked"),
        cls: "text-rose-400",
      };
    }
    if (row.expires_at && new Date(row.expires_at) < new Date()) {
      return {
        key: "expired",
        label: t("platformCredentials.statusExpired"),
        cls: "text-amber-400",
      };
    }
    return {
      key: "active",
      label: t("platformCredentials.statusActive"),
      cls: "text-emerald-400",
    };
  };

  if (!isOwner) {
    return (
      <div className="p-6">
        <PageHeader title={t("platformCredentials.title")} />
        <Card>
          <p className="text-sm text-slate-400">
            {t("platformCredentials.ownerOnly")}
          </p>
        </Card>
      </div>
    );
  }
  if (loading) return <Loading />;

  return (
    <div className="p-6 space-y-6">
      <PageHeader
        title={t("platformCredentials.title")}
        subtitle={t("platformCredentials.subtitle")}
      />

      <Card>
        <h3 className="font-semibold mb-3">
          {t("platformCredentials.mintTitle")}
        </h3>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <Label>{t("platformCredentials.platform")}</Label>
            <Select value={platform} onValueChange={setPlatform}>
              <SelectTrigger className={INPUT_CLS} data-testid="pc-platform">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PLATFORMS.map((p) => (
                  <SelectItem key={p} value={p}>{p}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex-1 min-w-[220px]">
            <Label>{t("platformCredentials.label")}</Label>
            <Input
              className={INPUT_CLS}
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder={t("platformCredentials.labelPlaceholder")}
            />
          </div>
          <div>
            <Label>{t("platformCredentials.expiresDays")}</Label>
            <Input
              className={INPUT_CLS}
              type="number"
              min={1}
              max={MAX_EXPIRES_DAYS}
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
            />
          </div>
          <Button disabled={busy} onClick={mint}>
            {t("platformCredentials.mint")}
          </Button>
        </div>
      </Card>

      <Card>
        <h3 className="font-semibold mb-3">
          {t("platformCredentials.existingTitle")}
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-start text-slate-400">
                <th className="py-2">{t("platformCredentials.platform")}</th>
                <th>{t("platformCredentials.label")}</th>
                <th>{t("platformCredentials.colClientId")}</th>
                <th>{t("platformCredentials.colCreated")}</th>
                <th>{t("platformCredentials.colExpires")}</th>
                <th>{t("platformCredentials.colLastUsed")}</th>
                <th>{t("platformCredentials.colStatus")}</th>
                <th>{t("platformCredentials.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const st = statusOf(row);
                return (
                  <tr key={row.id} className="border-t border-[#222838]">
                    <td className="py-2">{row.platform}</td>
                    <td>{row.label}</td>
                    <td><code className="text-xs">{row.client_id}</code></td>
                    <td>
                      {row.created_at ? fmtDate(row.created_at) : "—"}
                      {row.created_by && (
                        <span className="block text-xs text-slate-500">
                          {t("platformCredentials.createdBy", {
                            email: row.created_by,
                          })}
                        </span>
                      )}
                    </td>
                    <td>{row.expires_at ? fmtDate(row.expires_at) : "—"}</td>
                    <td>
                      {row.last_used_at
                        ? fmtDate(row.last_used_at)
                        : t("platformCredentials.never")}
                    </td>
                    <td className={st.cls}>{st.label}</td>
                    <td className="space-x-2 rtl:space-x-reverse whitespace-nowrap">
                      {!row.revoked_at && (
                        <>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy}
                            onClick={() => rotate(row)}
                          >
                            {t("platformCredentials.rotate")}
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy}
                            onClick={() => revoke(row)}
                          >
                            {t("platformCredentials.revoke")}
                          </Button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
              {!rows.length && (
                <tr>
                  <td colSpan={8} className="py-3 text-slate-400">
                    {t("platformCredentials.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Dialog
        open={!!reveal}
        onOpenChange={(o) => { if (!o) setReveal(null); }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {reveal?.rotated
                ? t("platformCredentials.revealTitleRotated")
                : t("platformCredentials.revealTitleNew")}
            </DialogTitle>
          </DialogHeader>
          <p className="text-sm text-amber-400">
            {t("platformCredentials.revealWarning")}
          </p>
          {reveal && (
            <CopyRow
              value={reveal.value}
              copyLabel={t("platformCredentials.copy")}
              copiedLabel={t("platformCredentials.copied")}
            />
          )}
          <DialogFooter>
            <Button onClick={() => setReveal(null)}>
              {t("platformCredentials.done")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
