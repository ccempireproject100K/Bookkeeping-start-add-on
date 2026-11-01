import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { useNiche } from "@/context/NicheContext";
import ReferralArt from "@/components/ReferralArt";
import { toast } from "sonner";

// Member referral page (Empire referral program, adopted law
// 2026-11-01). All numbers come from the ledger's summary/terms
// payload — this page hard-codes no rates, counts, or thresholds.
// [Site] is the visitor's niche x market face name (product_name from
// the app's existing face resolution), falling back to the house name.
const money = (c) => `$${((c || 0) / 100).toFixed(2)}`;
const pct = (r) => `${Math.round((r || 0) * 1000) / 10}%`;

export default function Referral() {
  const { t } = useTranslation();
  const { pack, market } = useNiche();
  const site = pack?.product_name || "Mind at Ease Solutions";
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    api.get("/referrals/me")
      .then(setData)
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Loading />;
  if (error) {
    return (
      <div className="p-6">
        <PageHeader title={t("referral.title", "Referrals")} />
        <Card><p className="text-sm text-red-300">{error}</p></Card>
      </div>
    );
  }

  const terms = data?.terms || {};
  const bands = terms.bands || [];
  const books = data?.sites?.books || {};
  const paying = books.paying_referrals || 0;
  const qualifyAt = terms.qualify_at || books.qualify_at || 4;
  const qualified = Boolean(books.qualified);
  const link = data?.link
    ? `${window.location.origin}${data.link}`
    : "";
  const referrals = (data?.referrals || [])
    .filter((r) => r.product === "books");
  const freeMonths = data?.free_months || [];
  const fb = data?.friend_benefit_received;

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(link);
      toast.success(t("referral.linkCopied", "Referral link copied"));
    } catch {
      toast.error(link);
    }
  };

  return (
    <div className="p-6 space-y-6" data-testid="referral-page">
      <PageHeader
        title={t("referral.headline", "Bring people in. Get paid for years.")}
        subtitle={t(
          "referral.subhead",
          "Share your {{site}} link. When someone joins through it and becomes a paying customer, they're your referral — and you earn a percentage of everything they actually pay, for up to three years.",
          { site })}
      />

      <ReferralArt pack={pack} />

      {!data?.referral_active && (
        <Card>
          <p className="text-sm text-amber-200" data-testid="referral-inactive">
            {t("referral.inactiveNote",
              "Your referral link is ready. Referrals switch on when you subscribe — or right away when you start through the card door.")}
          </p>
        </Card>
      )}

      <Card>
        <h3 className="font-semibold mb-2">{t("referral.yourLink", "Your referral link")}</h3>
        <div className="flex flex-wrap items-center gap-3">
          <code className="text-sm bg-[#0F1218] border border-[#222838] rounded px-3 py-2" data-testid="referral-link">
            {link}
          </code>
          <Button onClick={copyLink}>{t("referral.copy", "Copy link")}</Button>
          <span className="text-sm text-gray-400">
            {t("referral.yourCode", "Your code")}:{" "}
            <strong className="text-gray-200" data-testid="referral-code">{data?.code}</strong>
          </span>
        </div>
      </Card>

      <Card>
        <h3 className="font-semibold mb-2">{t("referral.howYouQualify", "How you qualify")}</h3>
        <p className="text-sm text-gray-300 mb-3">
          {t("referral.qualifyBody",
            "Your first three paying referrals don't earn cash yet — but your third earns you a free month. The moment your fourth paying referral pays, you qualify, and from that day forward you earn on all of them. Commissions start on your qualification day; payments made before that day don't earn commission — nothing is paid retroactively. Your first four referrals' clocks all start on your qualification day; every later referral's clock starts on their first payment. Your qualification month counts in full.")}
        </p>
        <div className="flex items-center gap-2" data-testid="referral-progress">
          {Array.from({ length: qualifyAt }).map((_, i) => (
            <div key={i}
              className={`h-3 flex-1 rounded ${i < Math.min(paying, qualifyAt) ? "bg-emerald-500" : "bg-[#222838]"}`} />
          ))}
          <span className="text-sm text-gray-300 ms-2">
            {Math.min(paying, qualifyAt)} / {qualifyAt}
          </span>
        </div>
        <p className="text-sm mt-2 text-gray-400">
          {qualified
            ? t("referral.qualifiedOn", "You qualified on {{date}}.", { date: (books.qualified_at || "").slice(0, 10) })
            : t("referral.notQualifiedYet", "Not qualified yet — {{count}} of {{goal}} paying referrals.", { count: paying, goal: qualifyAt })}
        </p>
        {freeMonths.length > 0 && (
          <p className="text-sm mt-2 text-emerald-300" data-testid="referral-free-month">
            {t("referral.freeMonthEarned", "Free month earned: {{start}} to {{end}} ({{plan}}).",
              { start: freeMonths[0].start.slice(0, 10), end: freeMonths[0].end.slice(0, 10), plan: freeMonths[0].plan || "" })}
          </p>
        )}
      </Card>

      <Card>
        <h3 className="font-semibold mb-2">{t("referral.whatYouEarn", "What you earn on each referral")}</h3>
        <ul className="text-sm text-gray-300 space-y-1">
          {bands.map((b, i) => {
            const from = i === 0 ? 1 : bands[i - 1].through_month + 1;
            return (
              <li key={b.through_month}>
                <strong>{t("referral.monthsRange", "Months {{from}}–{{to}}", { from, to: b.through_month })}:</strong>{" "}
                {pct(b.rate)} {t("referral.ofWhatTheyPay", "of what they pay")}
              </li>
            );
          })}
          {terms.bonus_band && (
            <li>
              <strong>{t("referral.fourthYear", "Fourth year (months 37–48)")}:</strong>{" "}
              {pct(terms.bonus_band.rate)} — {t("referral.fourthYearBody",
                "only for referrals you bring in while you have {{threshold}} or more active paying referrals. Those referrals keep their fourth year permanently, even if your count later drops; if your count dips below the threshold, you have {{grace}} months to climb back before new referrals stop earning it.",
                { threshold: terms.bonus_threshold, grace: terms.grace_months })}
            </li>
          )}
        </ul>
        <p className="text-sm text-gray-400 mt-3">
          {t("referral.onlyCollected",
            "You earn only on money actually collected — never on invoices or promises. If a payment is refunded or charged back, the commission on it is adjusted or reversed; if it was already paid out to you, it comes back as a negative adjustment on your next payout. Your commission on a referral lasts only while that referral is a paying customer: if they cancel, it stops; if they come back while their clock is still running, it picks up again where their clock has reached — the clock never restarts.")}
        </p>
      </Card>

      <Card>
        <h3 className="font-semibold mb-2">{t("referral.friendBenefit", "What your friend gets")}</h3>
        <p className="text-sm text-gray-300">
          {t("referral.friendBenefitBody",
            "Someone who joins through your link gets two weeks with the tier above their plan turned on — a real taste of the tier above. If they sign up at the top tier (Heavy), there's no tier above, so they get their first two months half off instead.")}
        </p>
        {fb && (
          <p className="text-sm mt-2 text-emerald-300" data-testid="referral-friend-benefit-received">
            {fb.type === "tier_up"
              ? t("referral.youGotTierUp", "You joined through a referral link: your {{tier}} tier is on for {{days}} days from your plan start.", { tier: fb.tier, days: fb.days || 14 })
              : t("referral.youGotHalfOff", "You joined through a referral link at the top tier: your first {{months}} paid months are half off.", { months: fb.months || 2 })}
          </p>
        )}
      </Card>

      {referrals.length > 0 && (
        <Card>
          <h3 className="font-semibold mb-2">{t("referral.yourReferrals", "Your referrals")}</h3>
          <ul className="text-sm text-gray-300 space-y-1">
            {referrals.map((r) => (
              <li key={r.account_id} className="flex flex-wrap gap-x-4">
                <span>{r.account_id}</span>
                <span>
                  {r.current_band_rate != null
                    ? t("referral.currentBand", "Currently earning {{rate}}", { rate: pct(r.current_band_rate) })
                    : t("referral.clockNotStarted", "Clock starts at qualification")}
                </span>
                {r.bonus_year_locked && (
                  <span className="text-emerald-300">{t("referral.bonusLocked", "Fourth year locked in")}</span>
                )}
                {r.friend_benefit && (
                  <span className="text-gray-400">
                    {r.friend_benefit.type === "tier_up"
                      ? t("referral.friendTierUp", "Friend benefit: {{days}} days at {{tier}}", { days: r.friend_benefit.days, tier: r.friend_benefit.tier })
                      : t("referral.friendHalfOff", "Friend benefit: first {{months}} months half off", { months: r.friend_benefit.months })}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card>
        <h3 className="font-semibold mb-2">{t("referral.whenPaid", "When and how you get paid")}</h3>
        <p className="text-sm text-gray-300 mb-2">
          {t("referral.clearing", "Earnings become payable {{days}} days after they're earned, and payouts are made monthly.", { days: terms.clearing_days || 60 })}
        </p>
        <ul className="text-sm text-gray-300 space-y-1">
          <li><strong>Stripe</strong> — {t("referral.railStripe", "sent to your connected Stripe account in the monthly batch.")}</li>
          <li><strong>{t("referral.railBank", "Bank transfer (Melio)")}</strong> — {t("referral.railBankBody", "sent to your bank account.")}</li>
          <li><strong>{t("referral.railCheck", "Paper check by mail (Melio)")}</strong> — {t("referral.railCheckBody",
            "a $2 fee is deducted from the payout. You'll always see the fee before you choose this option.")}</li>
        </ul>
        <p className="text-sm text-gray-400 mt-3" data-testid="referral-balances">
          {t("referral.balances", "Earned {{earned}} · Payable now {{payable}} · Paid out {{paid}}",
            { earned: money(books.earned_cents), payable: money(books.payable), paid: money(books.paid) })}
        </p>
      </Card>

      <Card>
        <h3 className="font-semibold mb-2">{t("referral.fairPlay", "Fair play")}</h3>
        <p className="text-sm text-gray-300">
          {t("referral.fairPlayBody",
            "Referring yourself doesn't count, and one payment never earns twice. Confirmed abuse — including duplicate accounts made to game the program — removes you from the referral program: three months for a first strike, permanently for a second. That's the referral program only — never your {{site}} account.",
            { site })}
        </p>
        {market?.name && (
          <p className="text-xs text-gray-500 mt-3">
            {site} · {market.name}
          </p>
        )}
      </Card>
    </div>
  );
}
