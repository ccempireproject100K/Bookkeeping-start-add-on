import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowRight, Gauge, Copy, FileCheck2, Sparkles, ScanSearch } from "lucide-react";
import { api } from "@/lib/api";
import { Card } from "@/components/Page";
import { fmtDate, fmtDateTime, num } from "@/lib/format";
import { gradeTone, TONE_CLASSES, sparkPoints } from "@/lib/cleanupConsole";

function Meter({ value, tone }) {
  return <div className="h-2 rounded-full bg-[#222838] overflow-hidden" role="progressbar"
    aria-valuenow={Math.round(value || 0)} aria-valuemin={0} aria-valuemax={100}>
    <div className={`h-full rounded-full ${TONE_CLASSES[tone].bar} transition-[width] duration-500 motion-reduce:transition-none`}
      style={{ width: `${Math.max(0, Math.min(100, value || 0))}%` }} />
  </div>;
}

function Skeleton() {
  return <Card className="mb-6" data-testid="data-quality-panel-loading">
    <div className="animate-pulse space-y-4 motion-reduce:animate-none">
      <div className="h-5 w-40 rounded bg-[#222838]" />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {[0, 1, 2, 3].map((i) => <div key={i} className="h-20 rounded-xl bg-[#1a1f2b]" />)}
      </div>
    </div>
  </Card>;
}

/** Data-quality panel: books health score beside the cleanup metrics.
 * Every figure comes from /dashboard/health-score and the read-only
 * /dashboard/data-quality aggregate. Phase 2: import runs are persisted
 * now, so "files cleaned" is a real count from when tracking began
 * (the tile says since when), with a weekly sparkline drawn ONLY when
 * real runs exist in at least two different weeks. The not-tracked
 * rendering remains for older backends that still report the metric
 * as unavailable. */
export default function DataQualityPanel() {
  const { t } = useTranslation();
  const [health, setHealth] = useState(null);
  const [quality, setQuality] = useState(null);
  const [failed, setFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let live = true;
    setFailed(false);
    Promise.allSettled([api.get("/dashboard/health-score"), api.get("/dashboard/data-quality")])
      .then(([h, q]) => {
        if (!live) return;
        if (h.status === "fulfilled") setHealth(h.value);
        if (q.status === "fulfilled") setQuality(q.value);
        if (h.status === "rejected" && q.status === "rejected") setFailed(true);
      });
    return () => { live = false; };
  }, [retryKey]);

  if (failed) return <Card className="mb-6 flex items-center justify-between gap-4" data-testid="data-quality-panel-error">
    <p className="text-sm text-gray-400">{t("cleanupConsole.loadError")}</p>
    <button onClick={() => setRetryKey((k) => k + 1)}
      className="px-3 py-1.5 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-300 text-sm">
      {t("cleanupConsole.retry")}
    </button>
  </Card>;
  if (!health && !quality) return <Skeleton />;

  const tone = health ? gradeTone(health.score) : "gray";
  const tiles = [
    { key: "pending", icon: Copy, label: t("cleanupConsole.duplicatesPending"),
      value: quality ? num(quality.duplicates_pending) : "—", testid: "dq-duplicates-pending" },
    { key: "files", icon: FileCheck2, label: t("cleanupConsole.filesCleaned"),
      value: quality?.files_cleaned_available ? num(quality.files_cleaned_this_month) : t("cleanupConsole.filesNotTracked"),
      hint: !quality ? null
        : !quality.files_cleaned_available ? t("cleanupConsole.filesNotTrackedHint")
        : quality.files_cleaned_tracking_since
          ? t("cleanupConsole.filesTrackedSince", { date: fmtDate(quality.files_cleaned_tracking_since) })
          : null,
      spark: quality ? sparkPoints(quality.import_runs_by_week) : null,
      testid: "dq-files-cleaned" },
    { key: "normalized", icon: Sparkles, label: t("cleanupConsole.recordsNormalized"),
      value: quality ? `${num(quality.normalized_records)} / ${num(quality.records_total)} · ${quality.normalized_percent}%` : "—",
      testid: "dq-normalized" },
    { key: "scan", icon: ScanSearch, label: t("cleanupConsole.lastScan"),
      value: quality?.last_scan_at ? fmtDateTime(quality.last_scan_at) : t("cleanupConsole.neverScanned"),
      testid: "dq-last-scan" },
  ];

  return <Card className="mb-6" data-testid="data-quality-panel">
    <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
      <div>
        <h3 className="font-display font-semibold flex items-center gap-2">
          <Gauge size={17} className="text-amber-400" />{t("cleanupConsole.panelTitle")}
        </h3>
        <p className="text-xs text-gray-500 mt-1">{t("cleanupConsole.panelSubtitle")}</p>
      </div>
      <Link to="/cleanup-console" data-testid="dq-open-console"
        className="inline-flex items-center gap-1.5 text-sm text-amber-400 hover:text-amber-300">
        {t("cleanupConsole.openConsole")}<ArrowRight size={14} />
      </Link>
    </div>
    <div className="grid grid-cols-1 md:grid-cols-[220px_1fr] gap-4">
      <div className={`rounded-xl border ${TONE_CLASSES[tone].ring} bg-[#10131a] p-4`}>
        <div className="text-xs uppercase tracking-wider text-gray-500">{t("cleanupConsole.healthScore")}</div>
        <div className={`mt-2 font-mono text-3xl font-semibold ${TONE_CLASSES[tone].text}`}>
          {health ? health.score : "—"}<span className="text-sm text-gray-500"> / 100</span>
        </div>
        <div className="mt-3"><Meter value={health?.score || 0} tone={tone} /></div>
        {health?.flags?.[0] && <p className="mt-3 text-[11px] leading-relaxed text-gray-500">{health.flags[0]}</p>}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        {tiles.map((tile) => <div key={tile.key} data-testid={tile.testid}
          className="rounded-xl border border-[#222838] bg-[#10131a] p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] uppercase tracking-wider text-gray-500">{tile.label}</span>
            <tile.icon size={15} className="text-gray-500 shrink-0" />
          </div>
          <div className="mt-2 text-lg font-semibold text-gray-100 break-words">{tile.value}</div>
          {tile.spark && <svg className="mt-2" width="96" height="24" viewBox="0 0 96 24"
            role="img" aria-label={t("cleanupConsole.filesTrendLabel")} data-testid="dq-files-spark">
            <polyline points={tile.spark} fill="none" stroke="#f59e0b" strokeWidth="1.5"
              strokeLinejoin="round" strokeLinecap="round" />
          </svg>}
          {tile.hint && <p className="mt-1 text-[11px] leading-relaxed text-gray-600">{tile.hint}</p>}
        </div>)}
      </div>
    </div>
  </Card>;
}
