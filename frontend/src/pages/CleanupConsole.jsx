import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  UploadCloud, FileUp, ShieldCheck, ShieldAlert, CheckCircle2, Copy, X,
  History, ScanSearch, Loader2, Undo2, Merge as MergeIcon, Sparkles,
  CalendarClock, SlidersHorizontal,
} from "lucide-react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { fmtDateTime, num } from "@/lib/format";
import {
  gradeTone, TONE_CLASSES, messHealth, confidenceLabelKey,
  visibleSuggestions, recordFieldRows, previewChangeRows,
  heldChangeRows, ruleStateRows, ruleLabelKey,
} from "@/lib/cleanupConsole";

const KINDS = ["customers", "vendors", "items"];
const FIELD_LABEL = {
  name: "cleanupConsole.fieldName", email: "cleanupConsole.fieldEmail",
  phone: "cleanupConsole.fieldPhone", address: "cleanupConsole.fieldAddress",
  price: "cleanupConsole.field", sku: "cleanupConsole.field", kind: "cleanupConsole.field",
};

function Meter({ value, tone, label }) {
  return <div className="h-2 rounded-full bg-[#222838] overflow-hidden" role="progressbar"
    aria-label={label} aria-valuenow={Math.round(value || 0)} aria-valuemin={0} aria-valuemax={100}>
    <div className={`h-full rounded-full ${TONE_CLASSES[tone].bar} transition-[width] duration-500 motion-reduce:transition-none`}
      style={{ width: `${Math.max(0, Math.min(100, value || 0))}%` }} />
  </div>;
}

function SkeletonCards({ count = 3 }) {
  return <div className="space-y-3" aria-hidden="true">
    {Array.from({ length: count }).map((_, i) => (
      <div key={i} className="card-surface p-5 animate-pulse motion-reduce:animate-none">
        <div className="h-4 w-1/3 rounded bg-[#222838] mb-3" />
        <div className="h-3 w-2/3 rounded bg-[#1a1f2b] mb-2" />
        <div className="h-3 w-1/2 rounded bg-[#1a1f2b]" />
      </div>))}
  </div>;
}

const selectCls = "h-9 rounded-md border border-[#222838] bg-[#14171E] px-2.5 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500/60";

/* ============================ IMPORT TAB ============================ */

const RULE_SOURCE_KEY = {
  profile: "cleanupConsole.ruleSourceProfile",
  run: "cleanupConsole.ruleSourceRun",
  default: "cleanupConsole.ruleSourceDefault",
};

function ImportTab() {
  const { t } = useTranslation();
  const [kind, setKind] = useState("customers");
  const [fileName, setFileName] = useState("");
  const [csvText, setCsvText] = useState("");
  const [dragging, setDragging] = useState(false);
  const [preview, setPreview] = useState(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [gateError, setGateError] = useState("");
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [result, setResult] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [profileId, setProfileId] = useState("");
  const [ruleOverrides, setRuleOverrides] = useState({});
  const [learnRules, setLearnRules] = useState(false);
  const fileRef = useRef(null);

  const labelFor = useCallback((rule) => {
    const k = ruleLabelKey(rule);
    return k ? t(k) : rule;
  }, [t]);

  const loadProfiles = useCallback(async () => {
    try {
      const r = await api.get("/import/profiles");
      setProfiles(r.profiles || []);
    } catch { setProfiles([]); }
  }, []);
  useEffect(() => { loadProfiles(); }, [loadProfiles]);

  const kindProfiles = useMemo(
    () => profiles.filter((p) => p.kind === kind), [profiles, kind]);
  const activeProfile = useMemo(
    () => kindProfiles.find((p) => p.id === profileId) || null,
    [kindProfiles, profileId]);

  const readFile = async (file) => {
    if (!file) return;
    setFileName(file.name);
    setCsvText(await file.text());
    setPreview(null); setResult(null); setGateError("");
  };

  const doPreview = async (overrides = ruleOverrides) => {
    if (!csvText) return;
    setPreviewBusy(true); setGateError(""); setResult(null);
    try {
      setPreview(await api.post("/import/cleanup-preview", {
        kind, csv_text: csvText,
        profile_id: profileId || undefined,
        rule_overrides: Object.keys(overrides).length ? overrides : undefined,
      }));
    } catch (e) { toast.error(errMsg(e)); }
    finally { setPreviewBusy(false); }
  };

  const toggleRule = (rule, enabled) => {
    const next = { ...ruleOverrides, [rule]: enabled };
    setRuleOverrides(next);
    doPreview(next);
  };

  const doImport = async (override = false) => {
    setImportBusy(true);
    try {
      const r = await api.post("/import/run", {
        kind, csv_text: csvText, email_summary: false,
        file_name: fileName || undefined,
        profile_id: profileId || undefined,
        rule_overrides: Object.keys(ruleOverrides).length ? ruleOverrides : undefined,
        learn_rules: !!(learnRules && profileId),
        override_schema: override,
        override_reason: override ? overrideReason.trim() : undefined,
      });
      setResult(r); setOverrideOpen(false); setGateError("");
      toast.success(t("cleanupConsole.importedTitle"));
      if (learnRules && profileId) loadProfiles();
    } catch (e) {
      if (!override && e?.response?.status === 422) {
        // The schema gate refused: show its plain-language reasons and
        // offer the audited override path instead of a dead end.
        setGateError(errMsg(e));
      } else {
        toast.error(errMsg(e));
      }
    } finally { setImportBusy(false); }
  };

  const health = messHealth(preview);
  const healthTone = health == null ? "gray" : gradeTone(health);
  const changes = useMemo(() => previewChangeRows(preview), [preview]);
  const heldRows = useMemo(() => heldChangeRows(preview), [preview]);
  const ruleRows = useMemo(() => ruleStateRows(preview), [preview]);
  const fixes = useMemo(() => {
    const out = [];
    for (const c of preview?.mess_report?.columns || []) {
      const iss = c.issues || {};
      if (iss.invalid_emails) out.push(t("cleanupConsole.fixInvalidEmails", { count: iss.invalid_emails }));
      if (iss.invalid_phones) out.push(t("cleanupConsole.fixInvalidPhones", { count: iss.invalid_phones }));
      if (c.empty) out.push(t("cleanupConsole.fixEmpty", { count: c.empty, column: c.column }));
      if (iss.mixed_date_formats) out.push(t("cleanupConsole.fixMixedDates", { column: c.column }));
    }
    return out;
  }, [preview, t]);

  return <div className="space-y-5">
    <Card>
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <label className="text-sm text-gray-400" htmlFor="cc-kind">{t("cleanupConsole.kind")}</label>
        <select id="cc-kind" data-testid="cleanup-kind" className={selectCls} value={kind}
          onChange={(e) => { setKind(e.target.value); setProfileId(""); setRuleOverrides({}); setLearnRules(false); setPreview(null); setResult(null); }}>
          {KINDS.map((k) => <option key={k} value={k}>{t(`cleanupConsole.${k}`)}</option>)}
        </select>
        <label className="text-sm text-gray-400" htmlFor="cc-profile">{t("cleanupConsole.profile")}</label>
        <select id="cc-profile" data-testid="cleanup-profile" className={selectCls} value={profileId}
          onChange={(e) => { setProfileId(e.target.value); setRuleOverrides({}); setLearnRules(false); setPreview(null); setResult(null); }}>
          <option value="">{t("cleanupConsole.profileNone")}</option>
          {kindProfiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </div>
      {activeProfile && Object.keys(activeProfile.cleanup_rules || {}).length > 0 && (
        <p className="mb-4 text-xs text-gray-500" data-testid="cleanup-profile-autoapplies">
          <span className="text-gray-400">{t("cleanupConsole.profileAutoApplies")}</span>{" "}
          {Object.entries(activeProfile.cleanup_rules)
            .map(([rule, on]) => `${labelFor(rule)}: ${on ? t("cleanupConsole.ruleOn") : t("cleanupConsole.ruleOff")}`)
            .join(" · ")}
        </p>)}
      <div
        data-testid="cleanup-dropzone"
        role="button" tabIndex={0} aria-label={t("cleanupConsole.dropTitle")}
        onClick={() => fileRef.current?.click()}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileRef.current?.click(); } }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); readFile(e.dataTransfer.files?.[0]); }}
        className={`rounded-xl border-2 border-dashed p-8 text-center cursor-pointer transition-colors motion-reduce:transition-none ${dragging ? "border-amber-400 bg-amber-500/10" : "border-[#2a3040] hover:border-amber-500/50 bg-[#10131a]"}`}>
        <UploadCloud size={28} className="mx-auto text-amber-400" />
        <p className="mt-3 font-display font-semibold">{fileName || t("cleanupConsole.dropTitle")}</p>
        <p className="mt-1 text-sm text-gray-500">{t("cleanupConsole.dropHint")}</p>
        <span className="mt-4 inline-flex items-center gap-2 text-sm text-amber-400">
          <FileUp size={15} />{t("cleanupConsole.chooseFile")}
        </span>
        <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden"
          data-testid="cleanup-file-input"
          onChange={(e) => readFile(e.target.files?.[0])} />
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button data-testid="cleanup-preview-btn" onClick={() => doPreview()} disabled={!csvText || previewBusy}>
          {previewBusy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <Sparkles size={15} />}
          {previewBusy ? t("cleanupConsole.previewing") : t("cleanupConsole.preview")}
        </Button>
        {previewBusy && <div className="flex-1 min-w-[160px]"><Meter value={65} tone="amber" label={t("cleanupConsole.previewing")} /></div>}
      </div>
    </Card>

    {previewBusy && <SkeletonCards count={2} />}

    {preview && !result && <div className="space-y-5 animate-rise motion-reduce:animate-none">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <div className="text-xs uppercase tracking-wider text-gray-500">{t("cleanupConsole.messHealth")}</div>
          <div className={`mt-2 font-mono text-3xl font-semibold ${TONE_CLASSES[healthTone].text}`}>
            {health}<span className="text-sm text-gray-500"> / 100</span>
          </div>
          <div className="mt-3"><Meter value={health} tone={healthTone} label={t("cleanupConsole.messHealth")} /></div>
          <p className="mt-3 text-xs text-gray-500">{num(preview.rows)} {t("cleanupConsole.rowsInFile")}</p>
        </Card>
        <Card className="md:col-span-2">
          <div className="flex items-center gap-2">
            {preview.can_import
              ? <ShieldCheck size={17} className="text-emerald-400" />
              : <ShieldAlert size={17} className="text-rose-400" />}
            <h3 className="font-display font-semibold">{t("cleanupConsole.schemaVerdict")}</h3>
          </div>
          {preview.can_import
            ? <p className="mt-2 text-sm text-emerald-300">{t("cleanupConsole.schemaPass")}</p>
            : <>
                <p className="mt-2 text-sm text-rose-300">{t("cleanupConsole.schemaBlocked")}</p>
                <ul className="mt-2 space-y-1.5 text-sm text-gray-300 list-disc ps-5">
                  {(preview.schema?.blocking || []).map((b, i) => <li key={i}>{b}</li>)}
                </ul>
              </>}
          {(preview.schema?.row_error_count || 0) > 0 &&
            <p className="mt-2 text-xs text-amber-300/90">{num(preview.schema.row_error_count)} {t("cleanupConsole.rowErrors")}</p>}
          {fixes.length > 0 && <ul className="mt-3 space-y-1.5 text-sm text-gray-400 list-disc ps-5">
            {fixes.map((f, i) => <li key={i}>{f}</li>)}
          </ul>}
        </Card>
      </div>

      <Card className="p-0 overflow-hidden">
        <div className="px-5 py-4 border-b border-[#222838]">
          <h3 className="font-display font-semibold">{t("cleanupConsole.column")} · {t("cleanupConsole.detectedType")}</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="cleanup-columns-table">
            <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.column")}</th>
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.detectedType")}</th>
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.empty")}</th>
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.issues")}</th>
            </tr></thead>
            <tbody>
              {(preview.mess_report?.columns || []).map((c) => <tr key={c.column} className="border-b border-[#171c26]">
                <td className="px-5 py-3 font-mono text-amber-300">{c.column}</td>
                <td className="px-5 py-3 text-gray-300">{preview.column_types?.[c.column]?.type || c.detected_type}</td>
                <td className="px-5 py-3 text-gray-400">{num(c.empty)}</td>
                <td className="px-5 py-3 text-gray-400">
                  {Object.keys(c.issues || {}).length
                    ? Object.entries(c.issues).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join(" · ")
                    : t("cleanupConsole.noIssues")}
                </td>
              </tr>)}
            </tbody>
          </table>
        </div>
      </Card>

      <Card className="p-0 overflow-hidden">
        <div className="px-5 py-4 border-b border-[#222838]">
          <h3 className="font-display font-semibold">{t("cleanupConsole.changesTitle")}</h3>
          <p className="text-xs text-gray-500 mt-1">{t("cleanupConsole.changesSubtitle")} {t("cleanupConsole.compareHint")}</p>
        </div>
        {changes.length === 0
          ? <p className="px-5 py-8 text-sm text-gray-500">{t("cleanupConsole.noChanges")}</p>
          : <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="cleanup-changes-table">
                <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-5 py-3 font-medium">{t("cleanupConsole.row")}</th>
                  <th className="px-5 py-3 font-medium">{t("cleanupConsole.field")}</th>
                  <th className="px-5 py-3 font-medium">{t("cleanupConsole.before")}</th>
                  <th className="px-5 py-3 font-medium">{t("cleanupConsole.after")}</th>
                  <th className="px-5 py-3 font-medium">{t("cleanupConsole.rule")}</th>
                  <th className="px-5 py-3 font-medium">{t("cleanupConsole.confidence")}</th>
                </tr></thead>
                <tbody>
                  {changes.map((ch, i) => <tr key={i} className="border-b border-[#171c26]">
                    <td className="px-5 py-3 text-gray-500">{ch.row}</td>
                    <td className="px-5 py-3 text-gray-300">{t(FIELD_LABEL[ch.field] || "cleanupConsole.field")}</td>
                    <td className="px-5 py-3 bg-rose-500/5 text-rose-200/90 font-mono text-xs break-all">{String(ch.before ?? "")}</td>
                    <td className="px-5 py-3 bg-emerald-500/5 text-emerald-200/90 font-mono text-xs break-all">{String(ch.after ?? "")}</td>
                    <td className="px-5 py-3 font-mono text-xs text-gray-400">{ch.rule}</td>
                    <td className="px-5 py-3 text-gray-300">{Math.round((ch.confidence || 0) * 100)}%</td>
                  </tr>)}
                </tbody>
              </table>
            </div>}
      </Card>

      {ruleRows.length > 0 && <Card data-testid="cleanup-rules-card">
        <div className="flex items-center gap-2">
          <SlidersHorizontal size={16} className="text-amber-400" />
          <h3 className="font-display font-semibold">{t("cleanupConsole.profileRulesTitle")}</h3>
        </div>
        <p className="mt-1 text-xs text-gray-500">{t("cleanupConsole.profileRulesHint")}</p>
        <ul className="mt-2 divide-y divide-[#171c26]">
          {ruleRows.map((rs) => <li key={rs.rule} className="flex items-center gap-3 py-2.5">
            <div className="flex-1 min-w-0">
              <div className="text-sm text-gray-200">{labelFor(rs.rule)}</div>
              <div className="text-[11px] text-gray-500">
                <span className="font-mono">{rs.rule}</span> · {t(RULE_SOURCE_KEY[rs.source] || RULE_SOURCE_KEY.default)}
              </div>
            </div>
            <input type="checkbox" className="accent-amber-500 h-4 w-4 shrink-0 cursor-pointer"
              aria-label={labelFor(rs.rule)}
              data-testid={`cleanup-rule-${rs.rule}`}
              checked={rs.enabled} disabled={previewBusy}
              onChange={(e) => toggleRule(rs.rule, e.target.checked)} />
          </li>)}
        </ul>
        {activeProfile && Object.keys(ruleOverrides).length > 0 && (
          <label className="mt-3 flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
            <input type="checkbox" className="accent-amber-500 h-4 w-4"
              data-testid="cleanup-learn-rules"
              checked={learnRules} onChange={(e) => setLearnRules(e.target.checked)} />
            {t("cleanupConsole.learnRules")}
          </label>)}
      </Card>}

      {(preview.held_in_first_50_rows || 0) > 0 && <Card className="p-0 overflow-hidden border-amber-500/40" data-testid="cleanup-held-card">
        <div className="px-5 py-4 border-b border-[#222838]">
          <h3 className="font-display font-semibold">{t("cleanupConsole.heldTitle")}</h3>
          <p className="text-xs text-gray-500 mt-1">{t("cleanupConsole.heldSubtitle")}</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="cleanup-held-table">
            <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.row")}</th>
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.field")}</th>
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.before")}</th>
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.after")}</th>
              <th className="px-5 py-3 font-medium">{t("cleanupConsole.rule")}</th>
            </tr></thead>
            <tbody>
              {heldRows.map((ch, i) => <tr key={i} className="border-b border-[#171c26]">
                <td className="px-5 py-3 text-gray-500">{ch.row}</td>
                <td className="px-5 py-3 text-gray-300">{t(FIELD_LABEL[ch.field] || "cleanupConsole.field")}</td>
                <td className="px-5 py-3 font-mono text-xs break-all text-gray-300">{String(ch.before ?? "")}</td>
                <td className="px-5 py-3 font-mono text-xs break-all text-gray-500">{String(ch.after ?? "")}</td>
                <td className="px-5 py-3">
                  <span className="font-mono text-xs text-gray-400">{ch.rule}</span>{" "}
                  <span className="px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-300 text-[10px] uppercase tracking-wide">{t("cleanupConsole.heldBadge")}</span>
                </td>
              </tr>)}
            </tbody>
          </table>
        </div>
      </Card>}

      {gateError && <Card className="border-rose-500/40">
        <p className="text-sm text-rose-300">{gateError}</p>
      </Card>}

      <div className="flex flex-wrap gap-3">
        <Button data-testid="cleanup-import-btn" onClick={() => doImport(false)} disabled={importBusy}>
          {importBusy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <CheckCircle2 size={15} />}
          {importBusy ? t("cleanupConsole.importing") : t("cleanupConsole.importNow")}
        </Button>
        {!preview.can_import && <Button variant="outline" data-testid="cleanup-override-open"
          onClick={() => setOverrideOpen(true)} disabled={importBusy}>
          {t("cleanupConsole.importAnyway")}
        </Button>}
      </div>
    </div>}

    {result && <Card className="animate-rise motion-reduce:animate-none" data-testid="cleanup-import-result">
      <div className="flex items-center gap-2">
        <CheckCircle2 size={18} className="text-emerald-400" />
        <h3 className="font-display font-semibold">{t("cleanupConsole.importedTitle")}</h3>
      </div>
      <div className="mt-4 grid grid-cols-2 lg:grid-cols-4 gap-3 text-sm">
        <div><div className="text-2xl font-semibold">{num(result.created)}</div><div className="text-xs text-gray-500">{t("cleanupConsole.created")}</div></div>
        <div><div className="text-2xl font-semibold">{num(result.duplicates_skipped)}</div><div className="text-xs text-gray-500">{t("cleanupConsole.duplicatesSkipped")}</div></div>
        <div><div className="text-2xl font-semibold">{num(result.error_count)}</div><div className="text-xs text-gray-500">{t("cleanupConsole.errors")}</div></div>
        <div><div className="text-2xl font-semibold">{num(result.cleanup?.changes || 0)}</div><div className="text-xs text-gray-500">{t("cleanupConsole.cleanupChanges")}</div></div>
      </div>
      {Object.keys(result.cleanup?.changes_by_rule || {}).length > 0 && <div className="mt-4">
        <div className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("cleanupConsole.byRule")}</div>
        <ul className="space-y-1 text-sm text-gray-300">
          {Object.entries(result.cleanup.changes_by_rule).map(([rule, count]) =>
            <li key={rule} className="flex justify-between gap-4"><span className="font-mono text-xs">{rule}</span><span>{num(count)}</span></li>)}
        </ul>
      </div>}
      {(result.cleanup?.held_changes || 0) > 0 &&
        <p className="mt-3 text-sm text-amber-300/90">{t("cleanupConsole.cleanupHeld", { count: result.cleanup.held_changes })}</p>}
      <Button className="mt-5" variant="outline" onClick={() => { setResult(null); setPreview(null); setCsvText(""); setFileName(""); }}>
        {t("cleanupConsole.importAnother")}
      </Button>
    </Card>}

    <Dialog open={overrideOpen} onOpenChange={setOverrideOpen}>
      <DialogContent className="bg-[#14171E] border-[#222838] text-gray-100">
        <DialogHeader>
          <DialogTitle>{t("cleanupConsole.overrideTitle")}</DialogTitle>
          <DialogDescription className="text-gray-400">{t("cleanupConsole.overrideBody")}</DialogDescription>
        </DialogHeader>
        <label className="text-sm text-gray-300" htmlFor="cc-override-reason">{t("cleanupConsole.overrideReason")}</label>
        <textarea id="cc-override-reason" data-testid="cleanup-override-reason" rows={3}
          className="w-full rounded-md border border-[#222838] bg-[#10131a] px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-amber-500/60"
          placeholder={t("cleanupConsole.overridePlaceholder")}
          value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)} />
        <DialogFooter>
          <Button variant="ghost" onClick={() => setOverrideOpen(false)}>{t("cleanupConsole.cancel")}</Button>
          <Button data-testid="cleanup-override-confirm" variant="destructive"
            disabled={!overrideReason.trim() || importBusy} onClick={() => doImport(true)}>
            {importBusy ? t("cleanupConsole.importing") : t("cleanupConsole.overrideConfirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}

/* ========================== DUPLICATES TAB ========================== */

function PairCard({ s, recA, recB, busy, onMerge, onDismiss, t }) {
  const rows = recordFieldRows(recA, recB);
  const ev = s.evidence || {};
  const scorePct = Math.round((s.score || 0) * 100);
  const tone = s.score >= 0.9 ? "emerald" : s.score >= 0.8 ? "amber" : "rose";
  const chip = "px-2 py-1 rounded-full bg-[#1a1f2b] border border-[#222838] text-[11px] text-gray-300";
  return <article tabIndex={0} data-testid={`dedupe-pair-${s.id}`}
    aria-label={`${s.a_name || ""} / ${s.b_name || ""}`}
    className={`card-surface p-5 transition-all duration-300 motion-reduce:transition-none focus:outline-none focus:ring-1 focus:ring-amber-500/60 ${busy ? "opacity-50 scale-[0.99]" : "animate-rise motion-reduce:animate-none"}`}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h3 className="font-display font-semibold truncate">{s.a_name || s.a_id} <span className="text-gray-600">/</span> {s.b_name || s.b_id}</h3>
        <p className="text-xs text-gray-500 mt-0.5">{t(`cleanupConsole.${s.entity}`)} · {t("cleanupConsole.engine")}: {s.engine}</p>
      </div>
      <div className="w-44 shrink-0">
        <div className="flex items-baseline justify-between">
          <span className="text-[11px] uppercase tracking-wider text-gray-500">{t("cleanupConsole.score")}</span>
          <span className={`font-mono text-sm ${TONE_CLASSES[tone].text}`}>{scorePct}%</span>
        </div>
        <div className="mt-1.5"><Meter value={scorePct} tone={tone} label={t("cleanupConsole.score")} /></div>
        <p className={`mt-1 text-[11px] ${TONE_CLASSES[tone].text}`}>{t(confidenceLabelKey(s.score))}</p>
      </div>
    </div>

    {ev.why && <p className="mt-3 text-sm text-gray-400"><span className="text-gray-500">{t("cleanupConsole.why")}: </span>{ev.why}</p>}
    <div className="mt-3 flex flex-wrap gap-1.5">
      {ev.name_similarity != null && <span className={chip}>{t("cleanupConsole.evidenceName")}: {Math.round(ev.name_similarity * 100)}%</span>}
      {ev.email_match != null && <span className={chip}>{t("cleanupConsole.evidenceEmail")}: {ev.email_match ? t("cleanupConsole.yes") : t("cleanupConsole.no")}</span>}
      {ev.phone_match != null && <span className={chip}>{t("cleanupConsole.evidencePhone")}: {ev.phone_match ? t("cleanupConsole.yes") : t("cleanupConsole.no")}</span>}
      {ev.address_similarity != null && <span className={chip}>{t("cleanupConsole.evidenceAddress")}: {Math.round(ev.address_similarity * 100)}%</span>}
    </div>

    <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
      {[["cleanupConsole.recordA", recA], ["cleanupConsole.recordB", recB]].map(([labelKey, rec], side) => (
        <div key={labelKey} className="rounded-xl border border-[#222838] bg-[#10131a] p-3.5">
          <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-2">{t(labelKey)}</div>
          {rec ? rows.map((r) => {
            const val = side === 0 ? r.a : r.b;
            return <div key={r.key} className={`flex justify-between gap-3 py-1 text-sm ${r.differs ? "bg-amber-500/5 -mx-1.5 px-1.5 rounded" : ""}`}>
              <span className="text-gray-500 shrink-0">{t(FIELD_LABEL[r.key])}</span>
              <span className={`text-end break-all ${r.differs ? "text-amber-200" : "text-gray-300"}`}>{val || "—"}</span>
            </div>;
          }) : <p className="text-sm text-gray-600">{t("cleanupConsole.recordMissing")}</p>}
        </div>))}
    </div>

    <div className="mt-4 flex flex-wrap gap-2">
      <Button size="sm" data-testid={`dedupe-merge-${s.id}`} onClick={() => onMerge(s)} disabled={busy}>
        <MergeIcon size={14} />{t("cleanupConsole.merge")}
      </Button>
      <Button size="sm" variant="outline" data-testid={`dedupe-dismiss-${s.id}`} onClick={() => onDismiss(s)} disabled={busy}>
        <X size={14} />{t("cleanupConsole.dismiss")}
      </Button>
    </div>
  </article>;
}

function DismissedCard({ s, busy, onRestore, t }) {
  const scorePct = Math.round((s.score || 0) * 100);
  return <article data-testid={`dedupe-dismissed-${s.id}`}
    className={`card-surface p-5 ${busy ? "opacity-50" : "animate-rise motion-reduce:animate-none"}`}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h3 className="font-display font-semibold truncate">{s.a_name || s.a_id} <span className="text-gray-600">/</span> {s.b_name || s.b_id}</h3>
        <p className="text-xs text-gray-500 mt-0.5">
          {t(`cleanupConsole.${s.entity}`)} · {t("cleanupConsole.score")}: {scorePct}%
          {s.dismissed_at && <> · {t("cleanupConsole.dismissedAt")}: {fmtDateTime(s.dismissed_at)}</>}
        </p>
      </div>
      <Button size="sm" variant="outline" data-testid={`dedupe-restore-${s.id}`}
        onClick={() => onRestore(s)} disabled={busy}>
        <Undo2 size={14} />{t("cleanupConsole.restore")}
      </Button>
    </div>
    {s.evidence?.why && <p className="mt-2 text-sm text-gray-400"><span className="text-gray-500">{t("cleanupConsole.why")}: </span>{s.evidence.why}</p>}
  </article>;
}

function DuplicatesTab() {
  const { t } = useTranslation();
  const [entity, setEntity] = useState("all");
  const [sort, setSort] = useState("score");
  const [threshold, setThreshold] = useState(85);
  const [suggestions, setSuggestions] = useState([]);
  const [dismissed, setDismissed] = useState([]);
  const [view, setView] = useState("pending");
  const [schedule, setSchedule] = useState(null);
  const [scanRuns, setScanRuns] = useState([]);
  const [records, setRecords] = useState({ customers: {}, vendors: {} });
  const [merges, setMerges] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const [mergeTarget, setMergeTarget] = useState(null);
  const [keepId, setKeepId] = useState(null);
  const [reverseTarget, setReverseTarget] = useState(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  const loadSchedule = useCallback(async () => {
    try {
      const [cfg, runs] = await Promise.all([
        api.get("/dedupe/schedule"), api.get("/dedupe/scan-runs")]);
      setSchedule(cfg);
      setScanRuns(runs.scan_runs || []);
    } catch { /* schedule card simply stays hidden on failure */ }
  }, []);

  const load = useCallback(async () => {
    setLoading(true); setLoadFailed(false);
    try {
      const [sc, sv, dc, dv, mc, mv, cust, vend] = await Promise.all([
        api.get("/dedupe/suggestions", { params: { entity: "customers", status: "pending" } }),
        api.get("/dedupe/suggestions", { params: { entity: "vendors", status: "pending" } }),
        api.get("/dedupe/suggestions", { params: { entity: "customers", status: "dismissed" } }),
        api.get("/dedupe/suggestions", { params: { entity: "vendors", status: "dismissed" } }),
        api.get("/dedupe/merges", { params: { entity: "customers" } }),
        api.get("/dedupe/merges", { params: { entity: "vendors" } }),
        api.get("/customers"), api.get("/vendors"),
      ]);
      setSuggestions([...(sc.suggestions || []), ...(sv.suggestions || [])]);
      setDismissed([...(dc.suggestions || []), ...(dv.suggestions || [])]);
      setMerges([...(mc.merges || []), ...(mv.merges || [])]);
      const index = (rows) => Object.fromEntries((rows || []).map((r) => [r.id, r]));
      setRecords({ customers: index(cust), vendors: index(vend) });
    } catch (e) {
      setLoadFailed(true);
    } finally { setLoading(false); }
    loadSchedule();
  }, [loadSchedule]);
  useEffect(() => { load(); }, [load]);

  const saveSchedule = async (patch) => {
    if (!schedule) return;
    const next = {
      enabled: schedule.enabled, frequency: schedule.frequency,
      entities: schedule.entities, ...patch,
    };
    try {
      setSchedule(await api.put("/dedupe/schedule", next));
      toast.success(t("cleanupConsole.scheduleSaved"));
    } catch (e) { toast.error(errMsg(e)); }
  };

  const visible = useMemo(
    () => visibleSuggestions(suggestions, { entity, minScore: threshold / 100, sort }),
    [suggestions, entity, sort, threshold]);
  const belowThreshold = useMemo(
    () => suggestions.filter((s) => (entity === "all" || s.entity === entity) && (s.score || 0) < threshold / 100),
    [suggestions, entity, threshold]);

  const runScan = async () => {
    setScanning(true);
    try {
      const kinds = entity === "all" ? ["customers", "vendors"] : [entity];
      let scanned = 0, found = 0;
      for (const k of kinds) {
        const r = await api.post("/dedupe/scan", { entity: k });
        scanned += r.scanned || 0; found += (r.suggestions || []).length;
      }
      toast.success(t("cleanupConsole.scanResult", { count: scanned, found }));
      await load();
    } catch (e) { toast.error(errMsg(e)); }
    finally { setScanning(false); }
  };

  const doMerge = async () => {
    const s = mergeTarget; if (!s) return;
    setBusyId(s.id);
    try {
      const r = await api.post(`/dedupe/suggestions/${s.id}/merge`, { survivor_id: keepId || undefined });
      setMergeTarget(null);
      toast.success(t("cleanupConsole.mergedToast"), {
        action: { label: t("cleanupConsole.undo"), onClick: () => undoMerge(r.merge_id) },
      });
      await load();
    } catch (e) { toast.error(errMsg(e)); }
    finally { setBusyId(null); }
  };

  const restorePair = async (s, quiet = false) => {
    setBusyId(s.id);
    try {
      await api.post(`/dedupe/suggestions/${s.id}/restore`);
      if (!quiet) toast.success(t("cleanupConsole.restoredToast"));
      await load();
    } catch (e) { toast.error(errMsg(e)); }
    finally { setBusyId(null); }
  };

  const doDismiss = async (s) => {
    setBusyId(s.id);
    try {
      await api.post(`/dedupe/suggestions/${s.id}/dismiss`);
      toast.success(t("cleanupConsole.dismissedToast"), {
        action: { label: t("cleanupConsole.undo"), onClick: () => restorePair(s, true) },
      });
      setSuggestions((cur) => cur.filter((x) => x.id !== s.id));
      setDismissed((cur) => [{ ...s, status: "dismissed" }, ...cur]);
    } catch (e) { toast.error(errMsg(e)); }
    finally { setBusyId(null); }
  };

  const dismissBelow = async () => {
    setBulkBusy(true);
    let done = 0;
    try {
      for (const s of belowThreshold) {
        await api.post(`/dedupe/suggestions/${s.id}/dismiss`);
        done += 1;
      }
      toast.success(t("cleanupConsole.dismissedMany", { count: done }));
      await load();
    } catch (e) {
      toast.error(errMsg(e));
      await load();
    } finally { setBulkBusy(false); }
  };

  const undoMerge = async (mergeId) => {
    try {
      await api.post(`/dedupe/merges/${mergeId}/reverse`);
      toast.success(t("cleanupConsole.undoneToast"));
      await load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const visibleDismissed = useMemo(
    () => dismissed.filter((s) => entity === "all" || s.entity === entity),
    [dismissed, entity]);

  return <div className="space-y-5">
    {schedule && <Card data-testid="dedupe-schedule-card">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-display font-semibold flex items-center gap-2">
            <CalendarClock size={16} className="text-amber-400" />{t("cleanupConsole.scheduleTitle")}
          </h3>
          <p className="mt-1 text-xs text-gray-500 max-w-xl">{t("cleanupConsole.scheduleHint")}</p>
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-200 cursor-pointer shrink-0">
          <input type="checkbox" className="accent-amber-500 h-4 w-4"
            data-testid="dedupe-schedule-enabled"
            checked={!!schedule.enabled}
            onChange={(e) => saveSchedule({ enabled: e.target.checked })} />
          {t("cleanupConsole.scheduleEnable")}
        </label>
      </div>
      <div className="mt-3 flex flex-wrap items-end gap-4">
        <div>
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="cc-frequency">{t("cleanupConsole.scheduleFrequency")}</label>
          <select id="cc-frequency" data-testid="dedupe-schedule-frequency" className={selectCls}
            value={schedule.frequency} disabled={!schedule.enabled}
            onChange={(e) => saveSchedule({ frequency: e.target.value })}>
            <option value="daily">{t("cleanupConsole.freqDaily")}</option>
            <option value="weekly">{t("cleanupConsole.freqWeekly")}</option>
          </select>
        </div>
        <div className="text-sm">
          <div className="text-[11px] uppercase tracking-wider text-gray-500">{t("cleanupConsole.lastScan")}</div>
          <div className="text-gray-200 mt-0.5">{schedule.last_scan_at ? fmtDateTime(schedule.last_scan_at) : t("cleanupConsole.neverScanned")}</div>
        </div>
        <div className="text-sm">
          <div className="text-[11px] uppercase tracking-wider text-gray-500">{t("cleanupConsole.nextScan")}</div>
          <div className="text-gray-200 mt-0.5">
            {!schedule.enabled ? t("cleanupConsole.scheduleOff")
              : schedule.next_scan_at ? fmtDateTime(schedule.next_scan_at)
              : t("cleanupConsole.scheduleAwaiting")}
          </div>
        </div>
      </div>
      {scanRuns.length > 0 && <div className="mt-4 border-t border-[#222838] pt-3">
        <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-2">{t("cleanupConsole.scanRunsTitle")}</div>
        <ul className="space-y-1 text-sm text-gray-400">
          {scanRuns.slice(0, 5).map((r) => <li key={r.id} className="flex flex-wrap gap-x-3 gap-y-0.5">
            <span className="text-gray-300">{t(`cleanupConsole.${r.entity}`)}</span>
            <span>{r.trigger === "scheduled" ? t("cleanupConsole.triggerScheduled") : t("cleanupConsole.triggerManual")}</span>
            <span>{r.finished_at ? fmtDateTime(r.finished_at) : "—"}</span>
            <span className="text-gray-500">{t("cleanupConsole.scanRunFound", { count: r.suggestions_new || 0 })}</span>
          </li>)}
        </ul>
      </div>}
    </Card>}

    <Card>
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="cc-entity">{t("cleanupConsole.entity")}</label>
          <select id="cc-entity" data-testid="dedupe-entity" className={selectCls} value={entity} onChange={(e) => setEntity(e.target.value)}>
            <option value="all">{t("cleanupConsole.all")}</option>
            <option value="customers">{t("cleanupConsole.customers")}</option>
            <option value="vendors">{t("cleanupConsole.vendors")}</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="cc-sort">{t("cleanupConsole.sort")}</label>
          <select id="cc-sort" data-testid="dedupe-sort" className={selectCls} value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="score">{t("cleanupConsole.sortScore")}</option>
            <option value="recent">{t("cleanupConsole.sortRecent")}</option>
            <option value="name">{t("cleanupConsole.sortName")}</option>
          </select>
        </div>
        <div className="min-w-[180px] flex-1 sm:flex-none">
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="cc-threshold">
            {t("cleanupConsole.minConfidence")}: {threshold}%
          </label>
          <input id="cc-threshold" data-testid="dedupe-threshold" type="range" min={50} max={99} step={1}
            value={threshold} onChange={(e) => setThreshold(Number(e.target.value))}
            className="w-full accent-amber-500" />
        </div>
        <div className="flex flex-wrap gap-2 ms-auto">
          <Button variant="outline" data-testid="dedupe-dismiss-below" onClick={dismissBelow}
            disabled={bulkBusy || belowThreshold.length === 0}>
            {bulkBusy ? <Loader2 size={14} className="animate-spin motion-reduce:animate-none" /> : <X size={14} />}
            {t("cleanupConsole.dismissBelow", { percent: threshold })}
          </Button>
          <Button data-testid="dedupe-scan" onClick={runScan} disabled={scanning}>
            {scanning ? <Loader2 size={14} className="animate-spin motion-reduce:animate-none" /> : <ScanSearch size={14} />}
            {scanning ? t("cleanupConsole.scanning") : t("cleanupConsole.runScan")}
          </Button>
        </div>
      </div>
    </Card>

    <div className="flex gap-2" role="tablist" aria-label={t("cleanupConsole.tabDuplicates")}>
      {[["pending", t("cleanupConsole.viewQueue")],
        ["dismissed", `${t("cleanupConsole.viewDismissed")} (${dismissed.length})`]].map(([key, label]) => (
        <button key={key} role="tab" aria-selected={view === key} data-testid={`dedupe-view-${key}`}
          onClick={() => setView(key)}
          className={`px-3.5 py-1.5 rounded-lg text-sm transition-colors motion-reduce:transition-none ${view === key ? "bg-amber-500/15 text-amber-300 border border-amber-500/40" : "text-gray-400 border border-transparent hover:text-gray-100"}`}>
          {label}
        </button>))}
    </div>

    {loading && <SkeletonCards count={3} />}
    {!loading && loadFailed && <Card className="flex items-center justify-between gap-4">
      <p className="text-sm text-gray-400">{t("cleanupConsole.loadError")}</p>
      <Button variant="outline" size="sm" onClick={load}>{t("cleanupConsole.retry")}</Button>
    </Card>}
    {!loading && !loadFailed && view === "pending" && visible.length === 0 &&
      <Card className="p-10 text-center" data-testid="dedupe-empty">
        <Copy size={22} className="mx-auto text-gray-600" />
        <p className="mt-3 text-sm text-gray-500">{t("cleanupConsole.emptyQueue")}</p>
      </Card>}
    {view === "pending" && <div className="space-y-4">
      {visible.map((s) => <PairCard key={s.id} s={s} t={t}
        recA={records[s.entity]?.[s.a_id]} recB={records[s.entity]?.[s.b_id]}
        busy={busyId === s.id}
        onMerge={(x) => { setMergeTarget(x); setKeepId(x.a_id); }}
        onDismiss={doDismiss} />)}
    </div>}
    {!loading && !loadFailed && view === "dismissed" && visibleDismissed.length === 0 &&
      <Card className="p-10 text-center" data-testid="dedupe-dismissed-empty">
        <Undo2 size={22} className="mx-auto text-gray-600" />
        <p className="mt-3 text-sm text-gray-500">{t("cleanupConsole.dismissedEmpty")}</p>
      </Card>}
    {view === "dismissed" && <div className="space-y-4">
      {visibleDismissed.map((s) => <DismissedCard key={s.id} s={s} t={t}
        busy={busyId === s.id} onRestore={(x) => restorePair(x)} />)}
    </div>}

    <Card className="p-0 overflow-hidden">
      <div className="px-5 py-4 border-b border-[#222838] flex items-center gap-2">
        <History size={16} className="text-gray-400" />
        <h3 className="font-display font-semibold">{t("cleanupConsole.historyTitle")}</h3>
      </div>
      {merges.length === 0
        ? <p className="px-5 py-8 text-sm text-gray-500">{t("cleanupConsole.noMerges")}</p>
        : <ul className="divide-y divide-[#171c26]">
            {merges.map((m) => <li key={m.id} className="px-5 py-3.5 flex flex-wrap items-center gap-3">
              <div className="flex-1 min-w-[220px]">
                <div className="text-sm text-gray-200">
                  <span className="text-gray-500">{t("cleanupConsole.survivor")}: </span>{m.survivor_before?.name || m.survivor_id}
                  <span className="text-gray-600"> · </span>
                  <span className="text-gray-500">{t("cleanupConsole.absorbed")}: </span>{m.absorbed_before?.name || m.absorbed_id}
                </div>
                <div className="text-xs text-gray-600 mt-0.5">{t(`cleanupConsole.${m.entity}`)} · {fmtDateTime(m.created_at)}</div>
              </div>
              <span className={`px-2 py-0.5 rounded-full text-[11px] ${m.status === "active" ? "bg-emerald-500/10 text-emerald-300" : "bg-[#222838] text-gray-400"}`}>
                {m.status === "active" ? t("cleanupConsole.statusActive") : t("cleanupConsole.statusReversed")}
              </span>
              {m.status === "active" && <Button size="sm" variant="outline" data-testid={`dedupe-undo-${m.id}`}
                onClick={() => setReverseTarget(m)}>
                <Undo2 size={13} />{t("cleanupConsole.undo")}
              </Button>}
            </li>)}
          </ul>}
    </Card>

    <Dialog open={!!mergeTarget} onOpenChange={(o) => { if (!o) setMergeTarget(null); }}>
      <DialogContent className="bg-[#14171E] border-[#222838] text-gray-100">
        <DialogHeader>
          <DialogTitle>{t("cleanupConsole.mergeTitle")}</DialogTitle>
          <DialogDescription className="text-gray-400">{t("cleanupConsole.mergeBody")}</DialogDescription>
        </DialogHeader>
        {mergeTarget && <div className="space-y-2" role="radiogroup" aria-label={t("cleanupConsole.survivor")}>
          {[mergeTarget.a_id, mergeTarget.b_id].map((id, i) => (
            <label key={id} className={`flex items-center gap-3 rounded-xl border p-3 cursor-pointer ${keepId === id ? "border-amber-500/50 bg-amber-500/5" : "border-[#222838] bg-[#10131a]"}`}>
              <input type="radio" name="cc-keep" className="accent-amber-500" checked={keepId === id} onChange={() => setKeepId(id)} />
              <span className="text-sm">{i === 0 ? mergeTarget.a_name : mergeTarget.b_name}</span>
              <span className="text-xs text-gray-500 ms-auto">{t("cleanupConsole.keepThis")}</span>
            </label>))}
        </div>}
        <DialogFooter>
          <Button variant="ghost" onClick={() => setMergeTarget(null)}>{t("cleanupConsole.cancel")}</Button>
          <Button data-testid="dedupe-merge-confirm" onClick={doMerge} disabled={busyId === mergeTarget?.id}>
            {busyId ? t("cleanupConsole.loading") : t("cleanupConsole.merge")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={!!reverseTarget} onOpenChange={(o) => { if (!o) setReverseTarget(null); }}>
      <DialogContent className="bg-[#14171E] border-[#222838] text-gray-100">
        <DialogHeader>
          <DialogTitle>{t("cleanupConsole.undo")}</DialogTitle>
          <DialogDescription className="text-gray-400">
            {reverseTarget?.survivor_before?.name} / {reverseTarget?.absorbed_before?.name}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setReverseTarget(null)}>{t("cleanupConsole.cancel")}</Button>
          <Button data-testid="dedupe-undo-confirm" onClick={async () => { const m = reverseTarget; setReverseTarget(null); await undoMerge(m.id); }}>
            <Undo2 size={14} />{t("cleanupConsole.undo")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}

/* ================================ PAGE ================================ */

export default function CleanupConsole() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "duplicates" ? "duplicates" : "import";
  const tabBtn = (key, label) => (
    <button key={key} role="tab" aria-selected={tab === key} data-testid={`cleanup-tab-${key}`}
      onClick={() => setParams(key === "duplicates" ? { tab: "duplicates" } : {})}
      className={`px-4 py-2 rounded-lg text-sm transition-colors motion-reduce:transition-none ${tab === key ? "bg-amber-500/15 text-amber-300 border border-amber-500/40" : "text-gray-400 border border-transparent hover:text-gray-100"}`}>
      {label}
    </button>);
  return <div className="space-y-6 max-w-5xl mx-auto animate-rise motion-reduce:animate-none">
    <PageHeader title={t("cleanupConsole.title")} subtitle={t("cleanupConsole.subtitle")} testid="cleanup-console-page" />
    <div className="flex gap-2" role="tablist" aria-label={t("cleanupConsole.title")}>
      {tabBtn("import", t("cleanupConsole.tabImport"))}
      {tabBtn("duplicates", t("cleanupConsole.tabDuplicates"))}
    </div>
    {tab === "import" ? <ImportTab /> : <DuplicatesTab />}
  </div>;
}
