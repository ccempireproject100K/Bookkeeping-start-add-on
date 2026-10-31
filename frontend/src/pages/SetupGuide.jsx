/**
 * SetupGuide — the AI expert guide (phase 3): a conversation that
 * works the setup checklist. The backend (routers/guide.py) runs a
 * scripted interview over the product's existing machinery; this
 * page is the conversation surface plus the character slot
 * (GuideCharacter) walking along the bottom.
 *
 * Honesty, rendered: without an AI key the page shows the same
 * unavailable state as the product's other AI features (the self-
 * serve checklist is unaffected); a flagged question is shown as
 * flagged, in the guide's own words; engine states surface as the
 * backend reports them — the page invents nothing.
 */
import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { Bot, Flag, Send, Upload } from 'lucide-react';
import { api } from '@/lib/api';
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import GuideCharacter from '@/components/GuideCharacter';
import { GUIDE_CHARACTER, inputKindForStep, answerBody } from '@/lib/guide';
import { giftErrorMessage } from '@/lib/welcomeGift';

export default function SetupGuide() {
  const { t } = useTranslation();
  const [phase, setPhase] = useState('loading'); // loading | unavailable | chat
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [step, setStep] = useState(null);
  const [charState, setCharState] = useState('idle');
  const [progress, setProgress] = useState(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState('');
  const [kind, setKind] = useState('customers');
  const [csvText, setCsvText] = useState('');
  const [est, setEst] = useState({ netProfit: '', filingStatus: 'single', w2Wages: '' });
  const bottomRef = useRef(null);

  const apply = (resp, echo) => {
    setMessages((m) => [
      ...m,
      ...(echo ? [{ from: 'user', text: echo }] : []),
      { from: 'guide', text: resp.message, flagged: resp.flagged },
    ]);
    setStep(resp.step);
    setCharState(resp.character_state || 'idle');
    setProgress(resp.progress);
    setDone(resp.done);
  };

  useEffect(() => {
    (async () => {
      try {
        const status = await api.get('/guide/status');
        if (!status.ai_configured) { setPhase('unavailable'); return; }
        const start = await api.post('/guide/start');
        setSessionId(start.session_id);
        setPhase('chat');
        apply(start);
      } catch (e) {
        if (e && e.response && e.response.status === 503) { setPhase('unavailable'); return; }
        setPhase('unavailable');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const el = bottomRef.current;
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages]);

  const send = async (input, echo) => {
    setBusy(true);
    try {
      const resp = await api.post('/guide/answer', answerBody(sessionId, step, input));
      apply(resp, echo);
      setText('');
      setCsvText('');
    } catch (e) {
      toast.error(giftErrorMessage(e, t('guide.sendFailed')));
    } finally { setBusy(false); }
  };

  if (phase === 'unavailable') {
    return (
      <div className="space-y-6" data-testid="guide-unavailable">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Bot className="h-6 w-6" /> {t('guide.title')}
        </h1>
        <Card>
          <CardContent className="pt-6 space-y-3">
            <p className="font-medium">{t('guide.unavailableTitle')}</p>
            <p className="text-sm text-muted-foreground">{t('guide.unavailableBody')}</p>
            <Link className="text-sm underline" to="/">{t('guide.backToChecklist')}</Link>
          </CardContent>
        </Card>
        <GuideCharacter state="error" character={GUIDE_CHARACTER} />
      </div>
    );
  }

  const inputKind = inputKindForStep(step);

  return (
    <div className="space-y-6" data-testid="setup-guide-page">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Bot className="h-6 w-6" /> {t('guide.title')}
        </h1>
        <p className="text-muted-foreground mt-1">{t('guide.subtitle')}</p>
        {progress && (
          <p className="text-sm text-muted-foreground mt-1" data-testid="guide-progress">
            {t('guide.progress', { done: progress.done, total: progress.total })}
          </p>
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">{t('guide.conversation')}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2 max-h-96 overflow-y-auto pr-1" data-testid="guide-messages">
            {messages.map((m, i) => (
              <div key={i}
                className={m.from === 'user'
                  ? 'ml-auto bg-[#1b2130] rounded-lg px-3 py-2 max-w-[85%] w-fit'
                  : 'mr-auto bg-[#14171E] border border-[#222838] rounded-lg px-3 py-2 max-w-[85%] w-fit'}
                data-testid={m.flagged ? 'guide-message-flagged' : `guide-message-${m.from}`}>
                {m.flagged && (
                  <span className="flex items-center gap-1 text-amber-400 text-xs font-medium">
                    <Flag className="h-3 w-3" /> {t('guide.flaggedNote')}
                  </span>
                )}
                <p className="text-sm whitespace-pre-wrap">{m.text}</p>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>

          {!done && phase === 'chat' && (
            <div className="border-t border-[#222838] pt-3 space-y-2">
              {inputKind === 'text' && (
                <div className="flex gap-2">
                  <Input data-testid="guide-text-input" value={text}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter' && text.trim()) send({ text }, text); }}
                    placeholder={t('guide.answerPlaceholder')} />
                  <Button data-testid="guide-send" disabled={busy || !text.trim()}
                    onClick={() => send({ text }, text)}>
                    <Send className="h-4 w-4" />
                  </Button>
                </div>
              )}
              {inputKind === 'file' && (
                <div className="space-y-2" data-testid="guide-file-input">
                  <div className="flex flex-wrap gap-2 items-end">
                    <div>
                      <Label>{t('guide.fileKind')}</Label>
                      <select className="flex h-9 rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                        value={kind} onChange={(e) => setKind(e.target.value)}>
                        <option value="customers">customers</option>
                        <option value="vendors">vendors</option>
                        <option value="items">items</option>
                      </select>
                    </div>
                    <div>
                      <Label>{t('guide.fileUpload')}</Label>
                      <Input type="file" accept=".csv,text/csv" data-testid="guide-file"
                        onChange={async (e) => {
                          const f = e.target.files && e.target.files[0];
                          if (f) setCsvText(await f.text());
                        }} />
                    </div>
                  </div>
                  <textarea className="w-full min-h-20 rounded-md border border-input bg-transparent px-3 py-2 text-sm font-mono"
                    data-testid="guide-csv" value={csvText}
                    onChange={(e) => setCsvText(e.target.value)}
                    placeholder={t('guide.csvPlaceholder')} />
                  <div className="flex gap-2">
                    <Button data-testid="guide-send-file" disabled={busy || !csvText.trim()}
                      onClick={() => send({ kind, csvText }, t('guide.sentFile'))}>
                      <Upload className="h-4 w-4 mr-1" /> {t('guide.sendFile')}
                    </Button>
                    <Button variant="ghost" data-testid="guide-skip" disabled={busy}
                      onClick={() => send({ skip: true }, 'skip')}>
                      {t('guide.skip')}
                    </Button>
                  </div>
                </div>
              )}
              {inputKind === 'estimate' && (
                <div className="flex flex-wrap gap-2 items-end" data-testid="guide-estimate-input">
                  <div>
                    <Label>{t('guide.netProfit')}</Label>
                    <Input type="number" data-testid="guide-est-profit" value={est.netProfit}
                      onChange={(e) => setEst({ ...est, netProfit: e.target.value })} />
                  </div>
                  <div>
                    <Label>{t('guide.w2Wages')}</Label>
                    <Input type="number" data-testid="guide-est-w2" value={est.w2Wages}
                      onChange={(e) => setEst({ ...est, w2Wages: e.target.value })} />
                  </div>
                  <Button data-testid="guide-send-estimate" disabled={busy || !est.netProfit}
                    onClick={() => send(est, `$${est.netProfit}`)}>
                    <Send className="h-4 w-4" />
                  </Button>
                </div>
              )}
            </div>
          )}
          {done && (
            <p className="text-sm text-emerald-400 border-t border-[#222838] pt-3" data-testid="guide-done">
              {t('guide.doneNote')} <Link className="underline" to="/">{t('guide.backToChecklist')}</Link>
            </p>
          )}
        </CardContent>
      </Card>

      <div className="border-t border-dashed border-[#222838] pt-2">
        <GuideCharacter state={charState} character={GUIDE_CHARACTER} />
      </div>
    </div>
  );
}
