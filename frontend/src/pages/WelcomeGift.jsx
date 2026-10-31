/**
 * WelcomeGift — the onboarding welcome gift (owner-approved
 * 2026-10-31): two free assets a new joiner keeps.
 *
 * Gift A cleans one uploaded file with the same DataClean engine path
 * the Studio uses — but on the tenant's own books session, so no
 * DataClean API key and no paid plan are needed. Gift B runs the tax
 * engine on a short form and hands back a downloadable estimate
 * packet. Both deliveries are recorded server-side, which is what
 * marks the onboarding checklist steps complete; both stay
 * re-runnable here afterwards.
 *
 * Honesty rules rendered, not just promised: a NOT_IMPLEMENTED
 * jurisdiction shows the engine's note verbatim before any number,
 * and an unbuilt engine shows an unavailable panel — never a zero.
 */
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Gift, Sparkles, Scale, Download, FileWarning, CheckCircle2 } from 'lucide-react';
import { api } from '@/lib/api';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  GIFT_KINDS, giftRecordsToCsv, duplicateReportText, downloadText,
  giftErrorCode, giftErrorMessage, estimateSummary, estimateBody,
} from '@/lib/welcomeGift';

const SAMPLE_CSV = [
  'name,email,phone,address',
  'Acme Supply Inc,ops@acme.example,(512) 555-0134,"9 Oak Ave, Dallas, TX 75001"',
  'ACME SUPPLY INC,ops@acme.example,512.555.0134,"9 Oak Ave, Dallas, TX 75001"',
  'Lone Star Plumbing,hello@lsp.example,(512) 555-0142,"77 Ranch Rd, Austin, TX 78701"',
].join('\n');

export default function WelcomeGift() {
  const { t } = useTranslation();
  const [meta, setMeta] = useState(null);

  // Gift A state
  const [kind, setKind] = useState('customers');
  const [csvText, setCsvText] = useState('');
  const [cleaning, setCleaning] = useState(false);
  const [cleanResult, setCleanResult] = useState(null);

  // Gift B state
  const [form, setForm] = useState({
    netProfit: '', filingStatus: 'single', state: '', w2Wages: '', paymentsMade: '',
  });
  const [estimating, setEstimating] = useState(false);
  const [estimate, setEstimate] = useState(null);
  const [engineDown, setEngineDown] = useState(false);

  const loadMeta = () => {
    api.get('/welcome-gift').then(setMeta).catch(() => setMeta(null));
  };
  useEffect(loadMeta, []);

  const gifts = (meta && meta.gifts) || {};
  const stubStates = (meta && meta.stub_states) || [];
  const filingStatuses = (meta && meta.filing_statuses) || [
    'single', 'married_filing_jointly', 'married_filing_separately', 'head_of_household'];

  const onFile = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) setCsvText(await f.text());
  };

  const runClean = async () => {
    if (!csvText.trim()) { toast.error(t('welcomeGift.needFile')); return; }
    setCleaning(true);
    try {
      const out = await api.post('/welcome-gift/clean', { kind, csv_text: csvText });
      setCleanResult(out);
      toast.success(t('welcomeGift.cleanDone'));
      loadMeta();
    } catch (e) {
      toast.error(giftErrorMessage(e, t('welcomeGift.cleanFailed')));
    } finally { setCleaning(false); }
  };

  const runEstimate = async () => {
    setEstimating(true);
    setEngineDown(false);
    setEstimate(null);
    try {
      const out = await api.post('/welcome-gift/estimate', estimateBody(form));
      setEstimate(out);
      toast.success(t('welcomeGift.estimateDone'));
      loadMeta();
    } catch (e) {
      if (giftErrorCode(e) === 'engine_not_built' || (e.response && e.response.status === 503)) {
        setEngineDown(true);
      } else {
        toast.error(giftErrorMessage(e, t('welcomeGift.estimateFailed')));
      }
    } finally { setEstimating(false); }
  };

  const summary = estimate ? estimateSummary(estimate.result) : null;
  const isStub = form.state && stubStates.includes(form.state.toUpperCase());

  return (
    <div className="space-y-6" data-testid="welcome-gift-page">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Gift className="h-6 w-6" /> {t('welcomeGift.title')}
        </h1>
        <p className="text-muted-foreground mt-1">{t('welcomeGift.subtitle')}</p>
      </div>

      {/* ------------------------------------------ gift A: clean */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-lg">
            <Sparkles className="h-5 w-5" /> {t('welcomeGift.cleanTitle')}
            {gifts.clean && gifts.clean.done && (
              <span className="text-sm font-normal text-green-700 flex items-center gap-1"
                data-testid="gift-clean-done">
                <CheckCircle2 className="h-4 w-4" /> {t('welcomeGift.doneKeep')}
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">{t('welcomeGift.cleanBody')}</p>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <Label>{t('welcomeGift.kind')}</Label>
              <select className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                data-testid="gift-clean-kind" value={kind}
                onChange={(e) => setKind(e.target.value)}>
                {GIFT_KINDS.map((k) => <option key={k} value={k}>{t(`welcomeGift.kind_${k}`)}</option>)}
              </select>
            </div>
            <div>
              <Label>{t('welcomeGift.file')}</Label>
              <Input type="file" accept=".csv,text/csv" data-testid="gift-clean-file" onChange={onFile} />
            </div>
            <Button variant="outline" data-testid="gift-clean-sample"
              onClick={() => setCsvText(SAMPLE_CSV)}>{t('welcomeGift.loadSample')}</Button>
          </div>
          <textarea className="w-full min-h-28 rounded-md border border-input bg-transparent px-3 py-2 text-sm font-mono"
            data-testid="gift-clean-csv" value={csvText}
            placeholder={t('welcomeGift.csvPlaceholder')}
            onChange={(e) => setCsvText(e.target.value)} />
          <Button onClick={runClean} disabled={cleaning} data-testid="gift-clean-run">
            {cleaning ? t('welcomeGift.cleaning') : t('welcomeGift.cleanRun')}
          </Button>

          {cleanResult && (
            <div className="space-y-3 border-t pt-3" data-testid="gift-clean-result">
              <p className="text-sm">
                {t('welcomeGift.cleanSummary', {
                  rows: cleanResult.rows,
                  changes: cleanResult.changes.length,
                  pairs: cleanResult.dedupe.pairs.length,
                })}
              </p>
              <div className="flex flex-wrap gap-2">
                <Button data-testid="gift-clean-download"
                  onClick={() => downloadText(giftRecordsToCsv(cleanResult.records, cleanResult.kind), `cleaned-${cleanResult.kind}.csv`)}>
                  <Download className="h-4 w-4 mr-1" /> {t('welcomeGift.downloadCleaned')}
                </Button>
                <Button variant="outline" data-testid="gift-report-download"
                  onClick={() => downloadText(duplicateReportText(cleanResult.dedupe.pairs, cleanResult.kind), 'duplicate-report.txt')}>
                  <Download className="h-4 w-4 mr-1" /> {t('welcomeGift.downloadReport')}
                </Button>
              </div>
              {cleanResult.dedupe.pairs.length > 0 && (
                <ul className="text-sm space-y-1" data-testid="gift-clean-pairs">
                  {cleanResult.dedupe.pairs.slice(0, 10).map((p, i) => (
                    <li key={i}>
                      {(p.records[0] && (p.records[0].name || p.records[0].id)) || '?'}
                      {' <> '}
                      {(p.records[1] && (p.records[1].name || p.records[1].id)) || '?'}
                      {' — '}{(p.evidence || []).join('; ')}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* --------------------------------------- gift B: estimate */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-lg">
            <Scale className="h-5 w-5" /> {t('welcomeGift.estimateTitle')}
            {gifts.estimate && gifts.estimate.done && (
              <span className="text-sm font-normal text-green-700 flex items-center gap-1"
                data-testid="gift-estimate-done">
                <CheckCircle2 className="h-4 w-4" /> {t('welcomeGift.doneKeep')}
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">{t('welcomeGift.estimateBody')}</p>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <Label>{t('welcomeGift.netProfit')}</Label>
              <Input type="number" data-testid="gift-est-net-profit" value={form.netProfit}
                onChange={(e) => setForm({ ...form, netProfit: e.target.value })} />
            </div>
            <div>
              <Label>{t('welcomeGift.filingStatus')}</Label>
              <select className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                data-testid="gift-est-filing-status" value={form.filingStatus}
                onChange={(e) => setForm({ ...form, filingStatus: e.target.value })}>
                {filingStatuses.map((s) => (
                  <option key={s} value={s}>{t(`welcomeGift.status_${s}`)}</option>
                ))}
              </select>
            </div>
            <div>
              <Label>{t('welcomeGift.state')}</Label>
              <Input data-testid="gift-est-state" maxLength={2} placeholder="CA"
                value={form.state}
                onChange={(e) => setForm({ ...form, state: e.target.value.toUpperCase() })} />
            </div>
            <div>
              <Label>{t('welcomeGift.w2Wages')}</Label>
              <Input type="number" data-testid="gift-est-w2" value={form.w2Wages}
                onChange={(e) => setForm({ ...form, w2Wages: e.target.value })} />
            </div>
            <div>
              <Label>{t('welcomeGift.paymentsMade')}</Label>
              <Input type="number" data-testid="gift-est-payments" value={form.paymentsMade}
                onChange={(e) => setForm({ ...form, paymentsMade: e.target.value })} />
            </div>
          </div>
          {isStub && (
            <p className="text-sm text-amber-700 flex items-center gap-1" data-testid="gift-est-stub-hint">
              <FileWarning className="h-4 w-4" /> {t('welcomeGift.stubHint', { state: form.state.toUpperCase() })}
            </p>
          )}
          <Button onClick={runEstimate} disabled={estimating} data-testid="gift-est-run">
            {estimating ? t('welcomeGift.estimating') : t('welcomeGift.estimateRun')}
          </Button>

          {engineDown && (
            <div className="border border-amber-300 bg-amber-50 rounded-md p-3 text-sm"
              data-testid="gift-est-unavailable">
              <p className="font-medium flex items-center gap-1">
                <FileWarning className="h-4 w-4" /> {t('welcomeGift.unavailableTitle')}
              </p>
              <p>{t('welcomeGift.unavailableBody')}</p>
            </div>
          )}

          {summary && (
            <div className="space-y-3 border-t pt-3" data-testid="gift-est-result">
              {summary.partial && summary.notes.length > 0 && (
                <div className="border border-amber-300 bg-amber-50 rounded-md p-3 text-sm"
                  data-testid="gift-est-notes">
                  <p className="font-medium">{t('welcomeGift.partialTitle')}</p>
                  {summary.notes.map((n, i) => <p key={i}>{n}</p>)}
                </div>
              )}
              <p className="text-lg" data-testid="gift-est-total">
                {summary.partial ? t('welcomeGift.totalPartial') : t('welcomeGift.total')}
                {': '}<strong>{summary.total}</strong>
              </p>
              <div>
                <p className="font-medium text-sm">{t('welcomeGift.breakdown')}</p>
                <ul className="text-sm">
                  {summary.breakdown.map((c) => (
                    <li key={c.key}>{c.label} ({c.jurisdiction}): {c.amount}</li>
                  ))}
                </ul>
              </div>
              <div>
                <p className="font-medium text-sm">{t('welcomeGift.schedule')}</p>
                <ul className="text-sm">
                  {summary.schedule.map((p) => (
                    <li key={p.quarter}>
                      {t('welcomeGift.quarter', { q: p.quarter })} — {p.dueDate}: {p.amount}
                    </li>
                  ))}
                </ul>
              </div>
              {summary.safeHarbor && (
                <p className="text-sm">
                  {t('welcomeGift.safeHarbor')}: {summary.safeHarbor.amount} ({summary.safeHarbor.basis})
                </p>
              )}
              <div>
                <p className="font-medium text-sm">{t('welcomeGift.workpapers')}</p>
                {summary.workpapers.map((w, i) => (
                  <div key={i} className="text-sm">
                    <p>{w.jurisdiction} — {w.config}{w.provisional ? ` · ${t('welcomeGift.provisional')}` : ''}</p>
                    <ul className="ml-4 list-disc">
                      {w.steps.map((s, j) => (
                        <li key={j}>{s.label}: {s.detail}{s.amount ? ` [${s.amount}]` : ''}</li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button data-testid="gift-est-download-pdf"
                  onClick={() => api.downloadCsv(`/welcome-gift/estimate/${estimate.delivery_id}/download?format=pdf`, 'first-estimate.pdf')}>
                  <Download className="h-4 w-4 mr-1" /> {t('welcomeGift.downloadPdf')}
                </Button>
                <Button variant="outline" data-testid="gift-est-download-csv"
                  onClick={() => api.downloadCsv(`/welcome-gift/estimate/${estimate.delivery_id}/download?format=csv`, 'first-estimate.csv')}>
                  <Download className="h-4 w-4 mr-1" /> {t('welcomeGift.downloadCsv')}
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
