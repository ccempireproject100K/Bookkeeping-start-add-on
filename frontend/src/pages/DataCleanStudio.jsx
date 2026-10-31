import { useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  UploadCloud, Sparkles, ShieldCheck, ShieldAlert, KeyRound, LogOut,
  Loader2, Eye, Download, ScanSearch, Wand2, Copy,
} from "lucide-react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { num } from "@/lib/format";
import { SAMPLE_CSV } from "@/lib/datacleanApi";
import {
  gradeTone, TONE_CLASSES, messHealth, previewChangeRows,
} from "@/lib/cleanupConsole";
import {
  KIND_FIELDS, STUDIO_CAPS, dedupeBatch, keyLooksValid, recordsToCsv,
  resolvePair, splitValues, cleanSummary,
} from "@/lib/datacleanStudio";

const selectCls = "h-9 rounded-md border border-[#222838] bg-[#14171E] px-2.5 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500/60";
const inputCls = "h-9 rounded-md border border-[#222838] bg-[#10131a] px-3 text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-amber-500/60";
const chip = "px-2 py-0.5 rounded-full bg-[#1a1f2b] border border-[#222838] text-[11px] text-gray-300";

function Meter({ value, tone, label }) {
  return <div className="h-2 rounded-full bg-[#222838] overflow-hidden" role="progressbar"
    aria-label={label} aria-valuenow={Math.round(value || 0)} aria-valuemin={0} aria-valuemax={100}>
    <div className={`h-full rounded-full ${TONE_CLASSES[tone].bar} transition-[width] duration-500 motion-reduce:transition-none`}
      style={{ width: `${Math.max(0, Math.min(100, value || 0))}%` }} />
  </div>;
}

/** The customer surface for the DataClean API. The API authenticates
 * an `mdck_` key, never a user JWT (backend/routers/dataclean_api.py),
 * so the customer connects with a key they issued on the DataClean API
 * page — the key lives in this tab's memory only and is never saved to
 * local storage or any collection. Every compute call below is a
 * metered key call; the caller's rows are cleaned in the request and
 * never stored (only the usage count is recorded). */
export default function DataCleanStudio() {
  const { t } = useTranslation();
  const [keyInput, setKeyInput] = useState("");
  const [apiKey, setApiKey] = useState(null);
  const [usage, setUsage] = useState(null);
  const [connectBusy, setConnectBusy] = useState(false);
  const [kind, setKind] = useState("customers");
  const [csvText, setCsvText] = useState("");
  const [fileName, setFileName] = useState("");
  const [preview, setPreview] = useState(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [clean, setClean] = useState(null);
  const [cleanBusy, setCleanBusy] = useState(false);
  const [pairs, setPairs] = useState(null);
  const [pairsTruncated, setPairsTruncated] = useState(false);
  const [scanBusy, setScanBusy] = useState(false);
  const [normType, setNormType] = useState("phone");
  const [normText, setNormText] = useState("");
  const [normResults, setNormResults] = useState(null);
  const [normBusy, setNormBusy] = useState(false);
  const fileRef = useRef(null);

  const dc = {
    get: (p) => api.get(p, { headers: { "X-API-Key": apiKey } }),
    post: (p, b) => api.post(p, b, { headers: { "X-API-Key": apiKey } }),
  };

  const connect = async () => {
    const key = keyInput.trim();
    if (!keyLooksValid(key)) { toast.error(t("datacleanStudio.invalidKey")); return; }
    setConnectBusy(true);
    try {
      const u = await api.get("/v1/dataclean/usage", { headers: { "X-API-Key": key } });
      setApiKey(key); setUsage(u); setKeyInput("");
      toast.success(t("datacleanStudio.connectedToast"));
    } catch (e) { toast.error(errMsg(e)); }
    finally { setConnectBusy(false); }
  };

  const disconnect = () => {
    setApiKey(null); setUsage(null); setPreview(null); setClean(null);
    setPairs(null); setNormResults(null);
  };

  const readFile = async (file) => {
    if (!file) return;
    setFileName(file.name);
    setCsvText(await file.text());
    setPreview(null); setClean(null); setPairs(null);
  };

  const doPreview = async () => {
    if (!csvText.trim()) return;
    setPreviewBusy(true);
    try { setPreview(await dc.post("/v1/dataclean/cleanup/preview", { kind, csv_text: csvText })); }
    catch (e) { toast.error(errMsg(e)); }
    finally { setPreviewBusy(false); }
  };

  const doClean = async () => {
    if (!csvText.trim()) return;
    setCleanBusy(true);
    try {
      const out = await dc.post("/v1/dataclean/cleanup/clean", { kind, csv_text: csvText });
      setClean(out); setPairs(null);
      const u = await dc.get("/v1/dataclean/usage").catch(() => null);
      if (u) setUsage(u);
    } catch (e) { toast.error(errMsg(e)); }
    finally { setCleanBusy(false); }
  };

  const doScan = async () => {
    if (!clean?.records) return;
    setScanBusy(true);
    try {
      const batch = dedupeBatch(clean.records, kind);
      const out = await dc.post("/v1/dataclean/dedupe/scan", { records: batch.records });
      setPairs(out.pairs || []); setPairsTruncated(batch.truncated);
    } catch (e) { toast.error(errMsg(e)); }
    finally { setScanBusy(false); }
  };

  const doNormalize = async () => {
    const values = splitValues(normText).slice(0, STUDIO_CAPS.maxNormalizeValues);
    if (!values.length) return;
    setNormBusy(true);
    try {
      const out = await dc.post("/v1/dataclean/normalize", { type: normType, values });
      setNormResults(out.results || []);
    } catch (e) { toast.error(errMsg(e)); }
    finally { setNormBusy(false); }
  };

  const downloadClean = () => {
    const csv = recordsToCsv(clean.records, kind);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `dataclean-${kind}-cleaned.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  };

  const health = messHealth(preview);
  const healthTone = health == null ? "gray" : gradeTone(health);
  const previewChanges = useMemo(() => previewChangeRows(preview || {}, 12), [preview]);
  const summary = useMemo(() => cleanSummary(clean), [clean]);
  const fields = KIND_FIELDS[kind];
  const cleanRecords = clean?.records || [];
  const shownRecords = cleanRecords.slice(0, 100);
  const shownChanges = (clean?.changes || []).slice(0, 50);

  return <div className="space-y-6 max-w-5xl mx-auto animate-rise motion-reduce:animate-none">
    <PageHeader title={t("datacleanStudio.title")} subtitle={t("datacleanStudio.subtitle")} testid="dataclean-studio-page" />

    {!apiKey && <Card>
      <h3 className="font-display font-semibold flex items-center gap-2">
        <KeyRound size={16} className="text-amber-400" />{t("datacleanStudio.connectTitle")}
      </h3>
      <p className="mt-1 text-sm text-gray-400 max-w-2xl">{t("datacleanStudio.connectBody")}</p>
      <div className="mt-4 flex flex-wrap gap-3">
        <input id="studio-key" data-testid="studio-key-input" type="password" autoComplete="off"
          className={`${inputCls} flex-1 min-w-[260px]`} value={keyInput}
          placeholder={t("datacleanStudio.keyPlaceholder")}
          onChange={(e) => setKeyInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") connect(); }} />
        <Button data-testid="studio-connect" onClick={connect} disabled={connectBusy || !keyInput.trim()}>
          {connectBusy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <KeyRound size={15} />}
          {connectBusy ? t("datacleanStudio.connecting") : t("datacleanStudio.connect")}
        </Button>
      </div>
      <p className="mt-3 text-xs text-gray-500">
        {t("datacleanStudio.keyMemoryNote")}{" "}
        <Link to="/dataclean-api" className="text-amber-400 hover:text-amber-300">{t("datacleanStudio.getKey")}</Link>
      </p>
    </Card>}

    {apiKey && <Card className="flex flex-wrap items-center justify-between gap-3" data-testid="studio-connected">
      <p className="text-sm text-gray-300">
        <span className="text-emerald-300">{t("datacleanStudio.connectedAs")}</span>{" "}
        <span className="font-mono text-amber-300/90">{usage?.key?.key_stub || "…"}</span>
        {usage && <span className="text-gray-500"> · {t("datacleanStudio.rowsThisMonth", { count: num(usage.rows_this_month) })}</span>}
      </p>
      <Button size="sm" variant="outline" data-testid="studio-disconnect" onClick={disconnect}>
        <LogOut size={13} />{t("datacleanStudio.disconnect")}
      </Button>
    </Card>}

    {apiKey && <>
      <Card className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm text-gray-400" htmlFor="studio-kind">{t("datacleanStudio.kind")}</label>
          <select id="studio-kind" data-testid="studio-kind" className={selectCls} value={kind}
            onChange={(e) => { setKind(e.target.value); setPreview(null); setClean(null); setPairs(null); }}>
            {Object.keys(KIND_FIELDS).map((k) => <option key={k} value={k}>{t(`cleanupConsole.${k}`)}</option>)}
          </select>
          <span className="text-xs text-gray-600">
            {t("datacleanStudio.capsNote", { rows: num(STUDIO_CAPS.maxRows) })}
          </span>
        </div>
        <div
          data-testid="studio-dropzone" role="button" tabIndex={0}
          aria-label={t("datacleanStudio.dropTitle")}
          onClick={() => fileRef.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileRef.current?.click(); } }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => { e.preventDefault(); readFile(e.dataTransfer.files?.[0]); }}
          className="rounded-xl border-2 border-dashed border-[#2a3040] hover:border-amber-500/50 bg-[#10131a] p-6 text-center cursor-pointer transition-colors motion-reduce:transition-none">
          <UploadCloud size={26} className="mx-auto text-amber-400" />
          <p className="mt-2 font-display font-semibold">{fileName || t("datacleanStudio.dropTitle")}</p>
          <p className="mt-1 text-sm text-gray-500">{t("datacleanStudio.dropHint")}</p>
          <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden"
            data-testid="studio-file-input" onChange={(e) => readFile(e.target.files?.[0])} />
        </div>
        <textarea data-testid="studio-csv" rows={7} value={csvText}
          onChange={(e) => { setCsvText(e.target.value); setFileName(""); }}
          placeholder={t("datacleanStudio.pasteHint")}
          className="w-full rounded-md border border-[#222838] bg-[#10131a] px-3 py-2 font-mono text-xs text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500/60" />
        <div className="flex flex-wrap gap-3">
          <Button data-testid="studio-preview" onClick={doPreview} disabled={!csvText.trim() || previewBusy} variant="outline">
            {previewBusy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <Eye size={15} />}
            {previewBusy ? t("datacleanStudio.previewing") : t("datacleanStudio.runPreview")}
          </Button>
          <Button data-testid="studio-clean" onClick={doClean} disabled={!csvText.trim() || cleanBusy}>
            {cleanBusy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <Sparkles size={15} />}
            {cleanBusy ? t("datacleanStudio.cleaning") : t("datacleanStudio.runClean")}
          </Button>
          <Button data-testid="studio-sample" variant="ghost"
            onClick={() => { setCsvText(SAMPLE_CSV); setFileName(""); setPreview(null); setClean(null); setPairs(null); }}>
            <Copy size={15} />{t("datacleanStudio.useSample")}
          </Button>
        </div>
      </Card>

      {preview && <section className="space-y-5 animate-rise motion-reduce:animate-none" data-testid="studio-preview-result">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Card>
            <div className="text-xs uppercase tracking-wider text-gray-500">{t("datacleanStudio.health")}</div>
            <div className={`mt-2 font-mono text-3xl font-semibold ${TONE_CLASSES[healthTone].text}`}>
              {health}<span className="text-sm text-gray-500"> / 100</span>
            </div>
            <div className="mt-3"><Meter value={health} tone={healthTone} label={t("datacleanStudio.health")} /></div>
            <p className="mt-3 text-xs text-gray-500">{num(preview.rows)} {t("datacleanStudio.rowsSeen")}</p>
          </Card>
          <Card className="md:col-span-2">
            <div className="flex items-center gap-2">
              {preview.can_import ? <ShieldCheck size={17} className="text-emerald-400" /> : <ShieldAlert size={17} className="text-rose-400" />}
              <h3 className="font-display font-semibold">{t("datacleanStudio.verdict")}</h3>
            </div>
            <p className={`mt-2 text-sm ${preview.can_import ? "text-emerald-300" : "text-rose-300"}`}>
              {preview.can_import ? t("datacleanStudio.verdictPass") : t("datacleanStudio.verdictBlocked")}
            </p>
            {(preview.schema?.blocking || []).length > 0 && (
              <ul className="mt-2 space-y-1.5 text-sm text-gray-300 list-disc ps-5">
                {(preview.schema.blocking || []).map((b, i) => <li key={i}>{b}</li>)}
              </ul>)}
            <p className="mt-3 text-sm text-gray-400">
              {t("datacleanStudio.changesLine", { count: preview.changes_in_first_50_rows })}
            </p>
          </Card>
        </div>
        {previewChanges.length > 0 && <Card className="p-0 overflow-hidden">
          <div className="px-5 py-4 border-b border-[#222838]">
            <h3 className="font-display font-semibold">{t("datacleanStudio.changesTitle")}</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="studio-preview-changes">
              <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colField")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colBefore")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colAfter")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colRule")}</th>
              </tr></thead>
              <tbody>
                {previewChanges.map((ch, i) => <tr key={i} className="border-b border-[#171c26]">
                  <td className="px-5 py-3 text-gray-300">{ch.field}</td>
                  <td className="px-5 py-3 bg-rose-500/5 text-rose-200/90 font-mono text-xs break-all">{String(ch.before ?? "")}</td>
                  <td className="px-5 py-3 bg-emerald-500/5 text-emerald-200/90 font-mono text-xs break-all">{String(ch.after ?? "")}</td>
                  <td className="px-5 py-3 font-mono text-xs text-gray-400">{ch.rule}</td>
                </tr>)}
              </tbody>
            </table>
          </div>
        </Card>}
      </section>}

      {clean && <section className="space-y-5 animate-rise motion-reduce:animate-none" data-testid="studio-clean-result">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <Card><div className="text-2xl font-semibold">{num(summary.rows)}</div><div className="text-xs text-gray-500">{t("datacleanStudio.rowsCleaned")}</div></Card>
          <Card><div className="text-2xl font-semibold">{num(summary.totalChanges)}</div><div className="text-xs text-gray-500">{t("datacleanStudio.changesMade")}</div></Card>
          <Card className="col-span-2">
            <div className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("datacleanStudio.byRuleTitle")}</div>
            {summary.byRule.length === 0
              ? <p className="text-sm text-gray-500">{t("datacleanStudio.noChanges")}</p>
              : <ul className="space-y-1 text-sm text-gray-300">
                  {summary.byRule.map(({ rule, count }) =>
                    <li key={rule} className="flex justify-between gap-4"><span className="font-mono text-xs">{rule}</span><span>{num(count)}</span></li>)}
                </ul>}
          </Card>
        </div>
        <div className="flex flex-wrap gap-3">
          <Button data-testid="studio-download" onClick={downloadClean} disabled={!cleanRecords.length}>
            <Download size={15} />{t("datacleanStudio.downloadCsv")}
          </Button>
          <Button data-testid="studio-scan" variant="outline" onClick={doScan} disabled={scanBusy || !cleanRecords.length}>
            {scanBusy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <ScanSearch size={15} />}
            {scanBusy ? t("datacleanStudio.scanning") : t("datacleanStudio.scanDuplicates")}
          </Button>
        </div>
        <Card className="p-0 overflow-hidden">
          <div className="px-5 py-4 border-b border-[#222838]">
            <h3 className="font-display font-semibold">{t("datacleanStudio.recordsTitle")}</h3>
            {cleanRecords.length > shownRecords.length &&
              <p className="text-xs text-gray-500 mt-1">{t("datacleanStudio.showingFirst", { shown: shownRecords.length, total: cleanRecords.length })}</p>}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="studio-records-table">
              <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                {fields.map((f) => <th key={f} className="px-5 py-3 font-medium">{f}</th>)}
              </tr></thead>
              <tbody>
                {shownRecords.map((r, i) => <tr key={i} className="border-b border-[#171c26]">
                  {fields.map((f) => <td key={f} className="px-5 py-3 text-gray-300 font-mono text-xs break-all">{String(r?.[f] ?? "")}</td>)}
                </tr>)}
              </tbody>
            </table>
          </div>
        </Card>
        {shownChanges.length > 0 && <Card className="p-0 overflow-hidden">
          <div className="px-5 py-4 border-b border-[#222838]">
            <h3 className="font-display font-semibold">{t("datacleanStudio.changesTitle")}</h3>
            {clean.changes.length > shownChanges.length &&
              <p className="text-xs text-gray-500 mt-1">{t("datacleanStudio.showingFirst", { shown: shownChanges.length, total: clean.changes.length })}</p>}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="studio-clean-changes">
              <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colRow")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colField")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colBefore")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colAfter")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanStudio.colRule")}</th>
              </tr></thead>
              <tbody>
                {shownChanges.map((ch, i) => <tr key={i} className="border-b border-[#171c26]">
                  <td className="px-5 py-3 text-gray-500">{ch.row}</td>
                  <td className="px-5 py-3 text-gray-300">{ch.field}</td>
                  <td className="px-5 py-3 bg-rose-500/5 text-rose-200/90 font-mono text-xs break-all">{String(ch.before ?? "")}</td>
                  <td className="px-5 py-3 bg-emerald-500/5 text-emerald-200/90 font-mono text-xs break-all">{String(ch.after ?? "")}</td>
                  <td className="px-5 py-3 font-mono text-xs text-gray-400">{ch.rule}</td>
                </tr>)}
              </tbody>
            </table>
          </div>
        </Card>}
      </section>}

      {pairs && <section className="space-y-4 animate-rise motion-reduce:animate-none" data-testid="studio-pairs-result">
        <h3 className="font-display font-semibold flex items-center gap-2">
          <ScanSearch size={16} className="text-amber-400" />{t("datacleanStudio.pairsTitle")}
        </h3>
        {pairsTruncated && <p className="text-sm text-amber-300/90">{t("datacleanStudio.pairsTruncated")}</p>}
        {pairs.length === 0
          ? <Card className="p-10 text-center"><p className="text-sm text-gray-500">{t("datacleanStudio.noPairs")}</p></Card>
          : pairs.map((pair, i) => {
              const p = resolvePair(pair, cleanRecords);
              const tone = p.score >= 0.9 ? "emerald" : p.score >= 0.8 ? "amber" : "rose";
              const ev = p.evidence;
              return <Card key={i} data-testid={`studio-pair-${i}`}>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h4 className="font-display font-semibold truncate">{p.aName} <span className="text-gray-600">/</span> {p.bName}</h4>
                    {ev.why && <p className="mt-1 text-sm text-gray-400"><span className="text-gray-500">{t("cleanupConsole.why")}: </span>{ev.why}</p>}
                  </div>
                  <span className={`font-mono text-sm ${TONE_CLASSES[tone].text}`}>{Math.round(p.score * 100)}%</span>
                </div>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {ev.name_similarity != null && <span className={chip}>{t("cleanupConsole.evidenceName")}: {Math.round(ev.name_similarity * 100)}%</span>}
                  {ev.email_match != null && <span className={chip}>{t("cleanupConsole.evidenceEmail")}: {ev.email_match ? t("cleanupConsole.yes") : t("cleanupConsole.no")}</span>}
                  {ev.phone_match != null && <span className={chip}>{t("cleanupConsole.evidencePhone")}: {ev.phone_match ? t("cleanupConsole.yes") : t("cleanupConsole.no")}</span>}
                  {ev.address_similarity != null && <span className={chip}>{t("cleanupConsole.evidenceAddress")}: {Math.round(ev.address_similarity * 100)}%</span>}
                </div>
              </Card>;
            })}
      </section>}

      <Card className="space-y-4">
        <div className="flex items-center gap-2">
          <Wand2 size={16} className="text-amber-400" />
          <h3 className="font-display font-semibold">{t("datacleanStudio.normalizeTitle")}</h3>
        </div>
        <p className="text-sm text-gray-400">{t("datacleanStudio.normalizeSubtitle")}</p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm text-gray-400" htmlFor="studio-norm-type">{t("datacleanStudio.normalizeType")}</label>
          <select id="studio-norm-type" data-testid="studio-norm-type" className={selectCls} value={normType}
            onChange={(e) => { setNormType(e.target.value); setNormResults(null); }}>
            {["phone", "date", "address"].map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <textarea data-testid="studio-norm-values" rows={4} value={normText}
          onChange={(e) => setNormText(e.target.value)}
          placeholder={t("datacleanStudio.normalizeValuesHint")}
          className="w-full rounded-md border border-[#222838] bg-[#10131a] px-3 py-2 font-mono text-xs text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500/60" />
        <div>
          <Button data-testid="studio-normalize" onClick={doNormalize} disabled={!splitValues(normText).length || normBusy}>
            {normBusy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <Wand2 size={15} />}
            {normBusy ? t("datacleanStudio.normalizing") : t("datacleanStudio.normalizeRun")}
          </Button>
        </div>
        {normResults && <div className="overflow-x-auto" data-testid="studio-norm-results">
          <table className="w-full text-sm">
            <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 font-medium">{t("datacleanStudio.colOriginal")}</th>
              <th className="px-5 py-3 font-medium">{t("datacleanStudio.colNormalized")}</th>
              <th className="px-5 py-3 font-medium">{t("datacleanStudio.colOk")}</th>
              <th className="px-5 py-3 font-medium">{t("datacleanStudio.colRule")}</th>
            </tr></thead>
            <tbody>
              {normResults.map((r, i) => <tr key={i} className="border-b border-[#171c26]">
                <td className="px-5 py-3 font-mono text-xs break-all text-gray-300">{String(r.original ?? "")}</td>
                <td className="px-5 py-3 font-mono text-xs break-all text-emerald-200/90">{String(r.normalized ?? "")}</td>
                <td className="px-5 py-3 text-sm">{r.ok ? "✓" : "—"}</td>
                <td className="px-5 py-3 font-mono text-xs text-gray-400">{r.rule}</td>
              </tr>)}
            </tbody>
          </table>
        </div>}
      </Card>
    </>}
  </div>;
}
