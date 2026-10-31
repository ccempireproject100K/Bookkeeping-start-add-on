import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  UploadCloud, Sparkles, ShieldCheck, ShieldAlert, Loader2, Eye, Lock,
} from "lucide-react";
import { api, errMsg } from "@/lib/api";
import LanguageSwitcher from "@/components/LanguageSwitcher";
import { FALLBACK_CAPS, SAMPLE_CSV } from "@/lib/datacleanApi";
import {
  gradeTone, TONE_CLASSES, messHealth, previewChangeRows,
} from "@/lib/cleanupConsole";

const KINDS = ["customers", "vendors", "items"];
const selectCls = "h-9 rounded-md border border-[#222838] bg-[#14171E] px-2.5 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500/60";

/** Public try-a-file playground for the DataClean API. Runs the REAL
 * engine through POST /api/v1/dataclean/playground/preview — the same
 * preview computation the paid surface serves. The endpoint persists
 * nothing (the response says so: persisted=false) and this page keeps
 * the file in local state only. Hard caps are the backend's own. */
export default function DataCleanDemo() {
  const { t } = useTranslation();
  const [kind, setKind] = useState("customers");
  const [csvText, setCsvText] = useState("");
  const [fileName, setFileName] = useState("");
  const [caps, setCaps] = useState(FALLBACK_CAPS);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const fileRef = useRef(null);

  useEffect(() => {
    document.title = `${t("datacleanDemo.title")} — Mind at Ease Solutions`;
    let live = true;
    api.get("/v1/dataclean/pricing")
      .then((p) => { if (live && p?.playground) setCaps(p.playground); })
      .catch(() => {});
    return () => { live = false; };
  }, [t]);

  const readFile = async (file) => {
    if (!file) return;
    setFileName(file.name);
    setCsvText(await file.text());
    setResult(null); setError("");
  };

  const run = async () => {
    if (!csvText.trim()) return;
    setBusy(true); setError("");
    try {
      setResult(await api.post("/v1/dataclean/playground/preview", { kind, csv_text: csvText }));
    } catch (e) {
      setResult(null);
      setError(errMsg(e));
      toast.error(errMsg(e));
    } finally { setBusy(false); }
  };

  const health = messHealth(result);
  const healthTone = health == null ? "gray" : gradeTone(health);
  const changes = useMemo(() => previewChangeRows(result || {}, 12), [result]);

  return <div className="min-h-screen bg-[#0d0f14] text-gray-100">
    <header className="border-b border-[#1c212e]">
      <div className="max-w-5xl mx-auto px-5 py-4 flex items-center justify-between gap-4">
        <Link to="/dataclean" className="font-display font-semibold tracking-tight">
          Mind at Ease <span className="text-amber-400">DataClean</span>
        </Link>
        <div className="flex items-center gap-3">
          <Link to="/dataclean" className="text-sm text-gray-400 hover:text-gray-100">{t("datacleanDemo.backToProduct")}</Link>
          <LanguageSwitcher />
        </div>
      </div>
    </header>

    <main className="max-w-5xl mx-auto px-5 py-10 space-y-6">
      <div className="animate-rise motion-reduce:animate-none">
        <h1 className="font-display text-3xl font-semibold">{t("datacleanDemo.title")}</h1>
        <p className="mt-2 text-gray-400 max-w-2xl">{t("datacleanDemo.subtitle")}</p>
        <p className="mt-3 inline-flex items-center gap-2 text-sm text-emerald-300 bg-emerald-500/10 border border-emerald-500/30 rounded-full px-3 py-1.5">
          <Lock size={13} />{t("datacleanDemo.nothingStored")}
        </p>
      </div>

      <section className="card-surface p-5 space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm text-gray-400" htmlFor="demo-kind">{t("datacleanDemo.kind")}</label>
          <select id="demo-kind" data-testid="demo-kind" className={selectCls} value={kind}
            onChange={(e) => { setKind(e.target.value); setResult(null); }}>
            {KINDS.map((k) => <option key={k} value={k}>{t(`cleanupConsole.${k}`)}</option>)}
          </select>
          <span className="text-xs text-gray-600">
            {t("datacleanDemo.caps", { kb: Math.round(caps.max_file_bytes / 1024), rows: caps.max_rows })}
          </span>
        </div>
        <div
          data-testid="demo-dropzone" role="button" tabIndex={0}
          aria-label={t("datacleanDemo.dropTitle")}
          onClick={() => fileRef.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileRef.current?.click(); } }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => { e.preventDefault(); readFile(e.dataTransfer.files?.[0]); }}
          className="rounded-xl border-2 border-dashed border-[#2a3040] hover:border-amber-500/50 bg-[#10131a] p-6 text-center cursor-pointer transition-colors motion-reduce:transition-none">
          <UploadCloud size={26} className="mx-auto text-amber-400" />
          <p className="mt-2 font-display font-semibold">{fileName || t("datacleanDemo.dropTitle")}</p>
          <p className="mt-1 text-sm text-gray-500">{t("datacleanDemo.dropHint")}</p>
          <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden"
            data-testid="demo-file-input" onChange={(e) => readFile(e.target.files?.[0])} />
        </div>
        <textarea data-testid="demo-csv" rows={7} value={csvText}
          onChange={(e) => { setCsvText(e.target.value); setFileName(""); }}
          placeholder={t("datacleanDemo.pasteHint")}
          className="w-full rounded-md border border-[#222838] bg-[#10131a] px-3 py-2 font-mono text-xs text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500/60" />
        <div className="flex flex-wrap gap-3">
          <button data-testid="demo-run" onClick={run} disabled={!csvText.trim() || busy}
            className="inline-flex items-center gap-2 h-10 px-4 rounded-lg bg-amber-500 text-black font-semibold text-sm disabled:opacity-50 hover:bg-amber-400 transition-colors motion-reduce:transition-none">
            {busy ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <Sparkles size={15} />}
            {busy ? t("datacleanDemo.running") : t("datacleanDemo.run")}
          </button>
          <button data-testid="demo-sample" onClick={() => { setCsvText(SAMPLE_CSV); setFileName(""); setResult(null); }}
            className="inline-flex items-center gap-2 h-10 px-4 rounded-lg border border-[#2a3040] text-sm text-gray-300 hover:text-gray-100 transition-colors motion-reduce:transition-none">
            <Eye size={15} />{t("datacleanDemo.useSample")}
          </button>
        </div>
      </section>

      {error && <section className="card-surface p-5 border-rose-500/40" data-testid="demo-error">
        <p className="text-sm text-rose-300">{error}</p>
      </section>}

      {result && <section className="space-y-5 animate-rise motion-reduce:animate-none" data-testid="demo-result">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="card-surface p-5">
            <div className="text-xs uppercase tracking-wider text-gray-500">{t("datacleanDemo.health")}</div>
            <div className={`mt-2 font-mono text-3xl font-semibold ${TONE_CLASSES[healthTone].text}`}>
              {health}<span className="text-sm text-gray-500"> / 100</span>
            </div>
            <div className="mt-3 h-2 rounded-full bg-[#222838] overflow-hidden" role="progressbar"
              aria-label={t("datacleanDemo.health")} aria-valuenow={health || 0} aria-valuemin={0} aria-valuemax={100}>
              <div className={`h-full rounded-full ${TONE_CLASSES[healthTone].bar} transition-[width] duration-500 motion-reduce:transition-none`}
                style={{ width: `${Math.max(0, Math.min(100, health || 0))}%` }} />
            </div>
            <p className="mt-3 text-xs text-gray-500">{result.rows} {t("datacleanDemo.rowsSeen")}</p>
          </div>
          <div className="card-surface p-5 md:col-span-2">
            <div className="flex items-center gap-2">
              {result.can_import
                ? <ShieldCheck size={17} className="text-emerald-400" />
                : <ShieldAlert size={17} className="text-rose-400" />}
              <h3 className="font-display font-semibold">{t("datacleanDemo.verdict")}</h3>
            </div>
            <p className={`mt-2 text-sm ${result.can_import ? "text-emerald-300" : "text-rose-300"}`}>
              {result.can_import ? t("datacleanDemo.verdictPass") : t("datacleanDemo.verdictBlocked")}
            </p>
            {(result.schema?.blocking || []).length > 0 && (
              <ul className="mt-2 space-y-1.5 text-sm text-gray-300 list-disc ps-5">
                {result.schema.blocking.map((b, i) => <li key={i}>{b}</li>)}
              </ul>)}
            <p className="mt-3 text-sm text-gray-400">
              {t("datacleanDemo.changesLine", { count: result.changes_in_first_50_rows })}
            </p>
          </div>
        </div>

        {changes.length > 0 && <div className="card-surface p-0 overflow-hidden">
          <div className="px-5 py-4 border-b border-[#222838]">
            <h3 className="font-display font-semibold">{t("datacleanDemo.changesTitle")}</h3>
            <p className="text-xs text-gray-500 mt-1">{t("datacleanDemo.changesSubtitle")}</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="demo-changes-table">
              <thead><tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 font-medium">{t("datacleanDemo.colField")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanDemo.colBefore")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanDemo.colAfter")}</th>
                <th className="px-5 py-3 font-medium">{t("datacleanDemo.colRule")}</th>
              </tr></thead>
              <tbody>
                {changes.map((ch, i) => <tr key={i} className="border-b border-[#171c26]">
                  <td className="px-5 py-3 text-gray-300">{ch.field}</td>
                  <td className="px-5 py-3 bg-rose-500/5 text-rose-200/90 font-mono text-xs break-all">{String(ch.before ?? "")}</td>
                  <td className="px-5 py-3 bg-emerald-500/5 text-emerald-200/90 font-mono text-xs break-all">{String(ch.after ?? "")}</td>
                  <td className="px-5 py-3 font-mono text-xs text-gray-400">{ch.rule}</td>
                </tr>)}
              </tbody>
            </table>
          </div>
        </div>}

        <p className="text-center text-sm text-gray-500">
          {t("datacleanDemo.ctaLine")} <Link to="/dataclean" className="text-amber-400 hover:text-amber-300">{t("datacleanDemo.ctaLink")}</Link>
        </p>
      </section>}
    </main>
  </div>;
}
