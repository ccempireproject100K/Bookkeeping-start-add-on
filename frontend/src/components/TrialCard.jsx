/**
 * TrialCard — the trial-doors panel in Settings (owner ruling
 * 2026-10-31). Shows the door, the trial state in plain words, the
 * exact first-month terms, and the actions: switch to the card door
 * (the opt-in), put a card on file, convert now, or cancel.
 *
 * Cancellation is dark-pattern-free by construction: the cancel
 * button ends the trial immediately; the reason buttons appear as an
 * OPTIONAL extra ("want to tell us why?") and skipping them changes
 * nothing. For a live subscription the reason is recorded here and
 * the actual cancel finishes in the Stripe portal, which is where
 * Stripe confirms it — the card says exactly that.
 *
 * Billing copy in Settings is hardcoded English by the file's own
 * precedent (billing copy changes faster than translations).
 */
import React, { useEffect, useState } from 'react';
import { api, errMsg } from '@/lib/api';
import { toast } from 'sonner';
import { Card } from '@/components/Page';
import { Button } from '@/components/ui/button';
import { CANCEL_REASONS, termsLine, trialStateLabel } from '@/lib/trial';

export default function TrialCard() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState('');
  const [showReasons, setShowReasons] = useState(false);
  const [note, setNote] = useState('');
  const [canceled, setCanceled] = useState(false);

  const load = () => api.get('/trial').then(setData).catch(() => setData(null));
  useEffect(() => { load(); }, []);

  if (!data || data.state === 'none') return null;

  const act = async (key, fn, okMsg) => {
    setBusy(key);
    try {
      const out = await fn();
      if (okMsg) toast.success(okMsg);
      return out;
    } catch (e) {
      toast.error(errMsg(e));
      return null;
    } finally { setBusy(''); }
  };

  const upgradeDoor = () => act('door', async () => {
    const out = await api.post('/trial/door', { door: 'card' });
    setData(out);
  }, 'Card door on — estimate gift, half-price first month and referrals are active');

  const addCard = () => act('card', async () => {
    const out = await api.post('/trial/card-session', { origin_url: window.location.origin });
    if (out && out.checkout_url) window.location.assign(out.checkout_url);
  });

  const convert = (plan) => act('convert', async () => {
    const out = await api.post('/trial/convert', { plan, origin_url: window.location.origin });
    if (out && out.checkout_url) window.location.assign(out.checkout_url);
  });

  const cancel = (reason) => act('cancel', async () => {
    const out = await api.post('/trial/cancel', { reason: reason || undefined, note: note || undefined });
    if (out && out.next === 'portal') {
      const portal = await api.post('/billing/portal-session', { origin_url: window.location.origin });
      if (portal && portal.url) { window.location.assign(portal.url); return out; }
    }
    setCanceled(true);
    load();
    return out;
  }, 'Canceled — thanks for trying it');

  const isCard = data.door === 'card';
  const live = data.state === 'trialing';

  return (
    <Card className="p-5" data-testid="trial-card">
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-medium text-gray-100">Your free trial</h3>
        <span className="text-xs text-gray-500" data-testid="trial-state">
          {trialStateLabel(data.state)}
          {live ? ` · ${data.trial_days_remaining} day(s) left` : ''}
        </span>
      </div>
      <p className="text-sm text-gray-400">
        {isCard
          ? 'You came in through the card door: the tax-estimate gift is unlocked, referrals are active, and your first paid month is half price.'
          : 'You came in through the no-card door: the file-cleaning gift is yours, and nothing will ever auto-charge.'}
      </p>
      <p className="text-sm text-gray-400 mt-1" data-testid="trial-referral">
        Referrals: {data.referral_active ? 'active — you can refer other businesses now' : 'switch on when you subscribe (or right away on the card door)'}
      </p>
      {data.state === 'expired' && (
        <p className="text-sm text-amber-400 mt-1">
          Your trial ended and nothing was charged. Your books are all still here — you're on the
          {' '}{data.terms && data.terms.medium ? 'plan shown below' : 'default plan'}; subscribe whenever you're ready.
          {isCard ? ' Your card-door half-price first month is still on record.' : ''}
        </p>
      )}
      <div className="mt-3 space-y-1 text-sm text-gray-300">
        {Object.values(data.terms || {}).map((tm) => (
          <div key={tm.plan} className="flex items-center justify-between gap-3">
            <span className="capitalize">{tm.plan}: {termsLine(tm)}</span>
            {data.state !== 'converted' && (
              <Button size="sm" variant="outline" data-testid={`trial-convert-${tm.plan}`}
                disabled={busy === 'convert'} onClick={() => convert(tm.plan)}>
                Subscribe
              </Button>
            )}
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {live && !isCard && (
          <Button size="sm" data-testid="trial-upgrade-door" disabled={busy === 'door'} onClick={upgradeDoor}>
            Switch to the card door (unlock the estimate + half-price first month)
          </Button>
        )}
        {live && isCard && !data.card_on_file && (
          <Button size="sm" variant="outline" data-testid="trial-add-card" disabled={busy === 'card'} onClick={addCard}>
            Add your card (no charge during the trial)
          </Button>
        )}
        {live && isCard && data.card_on_file && (
          <span className="text-xs text-emerald-400 self-center">Card on file — it auto-converts when the trial ends.</span>
        )}
        {data.state !== 'converted' && !canceled && (
          <Button size="sm" variant="ghost" data-testid="trial-cancel-open"
            onClick={() => (showReasons ? cancel(null) : setShowReasons(true))}>
            {showReasons ? 'Just cancel — no reason needed' : 'Cancel trial'}
          </Button>
        )}
      </div>
      {showReasons && !canceled && (
        <div className="mt-3 border-t border-[#222838] pt-3" data-testid="trial-cancel-reasons">
          <p className="text-sm text-gray-300 mb-2">
            Want to tell us why? One tap — or don't; canceling works either way, right now.
          </p>
          <div className="flex flex-wrap gap-2">
            {CANCEL_REASONS.map((r) => (
              <Button key={r} size="sm" variant="outline" data-testid={`trial-cancel-reason-${r}`}
                disabled={busy === 'cancel'} onClick={() => cancel(r)}>
                {r}
              </Button>
            ))}
          </div>
          <input className="mt-2 w-full bg-[#14171E] border border-[#222838] rounded-md text-sm px-3 py-2"
            data-testid="trial-cancel-note" placeholder="Anything else? (optional)"
            value={note} onChange={(e) => setNote(e.target.value)} />
          <Button size="sm" className="mt-2" data-testid="trial-cancel-with-note"
            disabled={busy === 'cancel'} onClick={() => cancel(null)}>
            Cancel with this note
          </Button>
        </div>
      )}
      {canceled && (
        <p className="text-sm text-gray-400 mt-3" data-testid="trial-canceled-note">
          Canceled. Your data stays put — sign back in any time.
        </p>
      )}
    </Card>
  );
}
