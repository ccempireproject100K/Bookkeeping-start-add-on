/**
 * Monster walkthrough tours — step definitions + pure state logic.
 *
 * Owner rule (2026-11-14): tours NEVER auto-start. They run only when the
 * user taps a "Show me how" button. Steps are written from the real built
 * UI — every `target` is a CSS selector for a control that exists on the
 * Invoices surface; a step with no matching element on screen simply shows
 * its card without a highlight (see MonsterTour).
 *
 * Language rule: plain, short, solo-operator words (gaps report #13 —
 * complexity is the top complaint; the tour is the antidote).
 */

export const TOURS = {
  "create-send": {
    title: "Create & send an invoice",
    tab: "invoices",
    steps: [
      {
        target: "[data-testid='btn-new-invoice']",
        title: "Start here",
        text: "Tap New Invoice. Pick the customer, add what you did, and the amount. That's the whole form.",
      },
      {
        target: null,
        title: "Save it",
        text: "Hit Save and the invoice lands in this list. Nothing goes to the customer yet — you send it when you're ready.",
      },
      {
        target: "[data-testid^='send-email-']",
        title: "Send it",
        text: "Tap the mail button on the invoice row to email it. Your customer gets a page where they can pay by bank or card — no login needed.",
      },
      {
        target: "[data-testid='invoice-assistant-panel']",
        title: "I keep an eye on it",
        text: "Once it's sent, your rundown up here counts it as waiting. When they open it or pay it, you'll see that here too.",
      },
    ],
  },
  "quote-first": {
    title: "Quote first, invoice in one tap",
    tab: "invoices",
    steps: [
      {
        target: null,
        title: "Start with a quote",
        text: "For bigger jobs, open Estimates in the left menu and make a quote first. Same lines, same client — nothing fancy.",
      },
      {
        target: null,
        title: "They say yes? One tap",
        text: "When the customer approves, tap Convert on the estimate. It becomes an invoice with the same lines, client, tax and terms — no retyping, ever.",
      },
      {
        target: "[data-testid='invoice-assistant-panel']",
        title: "Then it lands here",
        text: "The new invoice shows up in this list and in your rundown, ready to send like any other invoice.",
      },
    ],
  },
  "read-rundown": {
    title: "Read your rundown",
    tab: "invoices",
    steps: [
      {
        target: "[data-testid='assistant-mascot']",
        title: "This is your rundown",
        text: "Every time you open this page, I add up your invoices: what's open, what's waiting, what's overdue, and what got paid today.",
      },
      {
        target: "[data-testid='invoice-assistant-panel']",
        title: "The flags matter most",
        text: "Lines under the summary are things worth a look — an invoice opened a few times but not paid, a failed autopay, a payment that hit a snag. Tap a line and act on it.",
      },
      {
        target: "[data-testid='invoice-assistant-panel']",
        title: "Rundowns can come to you",
        text: "Want this summary on a schedule instead? Set it up under Invoice Settings — the last tour in the list shows you how.",
      },
    ],
  },
  autopay: {
    title: "Autopay, simply",
    tab: "invoices",
    steps: [
      {
        target: null,
        title: "Your customer turns it on",
        text: "On their pay page, a customer can save their bank or card and turn on autopay. You don't set it up for them.",
      },
      {
        target: "[data-testid^='autopay-charge-']",
        title: "You charge the saved method",
        text: "When an invoice has autopay, this card button charges the saved method. One tap, no chasing.",
      },
      {
        target: "[data-testid='invoice-assistant-panel']",
        title: "If it fails, I tell you why",
        text: "A declined card shows the reason right on the invoice, and I flag it in your rundown so it never sits quiet.",
      },
    ],
  },
  "sign-and-deposit": {
    title: "Contract, sign & deposit",
    tab: "invoices",
    steps: [
      {
        target: "[data-testid^='invoice-extras-']",
        title: "Put your agreement on the invoice",
        text: "Open an invoice (tap the little arrow on its row) and paste your agreement into the Agreement box. Save it. That's the contract your customer will sign.",
      },
      {
        target: "[data-testid^='split-']",
        title: "Split off the deposit",
        text: "Use the split button to break the invoice into Deposit + Balance. Your agreement rides along on the deposit — one signature covers the job.",
      },
      {
        target: null,
        title: "They sign, then they pay",
        text: "On their page, the customer reads the agreement, signs it right there (type or draw — no outside service, no extra cost), and only then can the deposit go through. You see the signature and its audit trail back here.",
      },
    ],
  },
  "design-shop": {
    title: "Design your invoice",
    tab: "settings",
    steps: [
      {
        target: "[data-testid='design-shop']",
        title: "This is the Design Shop",
        text: "Your logo, your colors, your photo — this is what your customers see when they open an invoice.",
      },
      {
        target: "[data-testid='design-accent-picker']",
        title: "Pick your color",
        text: "Tap the color box (or type the hex code) and pick the color your business uses. Buttons and totals wear it.",
      },
      {
        target: "[data-testid='design-layout']",
        title: "Choose a layout",
        text: "Classic keeps it simple, Modern centers everything, Bold puts your color across the top. Try each one.",
      },
      {
        target: "[data-testid='design-preview']",
        title: "Watch the preview",
        text: "This preview changes as you type — logo, photo, color, message. What you see here is what your customer sees.",
      },
      {
        target: "[data-testid='design-save']",
        title: "Save it",
        text: "Tap Save design and your look goes live on the customer page and in the invoice email. Change it any time.",
      },
    ],
  },
  "rundown-frequency": {
    title: "Set your rundown schedule",
    tab: "settings",
    steps: [
      {
        target: "[data-testid='rundown-settings']",
        title: "Rundowns, your call",
        text: "This card decides if I send you a scheduled rundown inside the app. It's off until you turn it on — no spam, ever.",
      },
      {
        target: "[data-testid='rundown-frequency']",
        title: "How often?",
        text: "Once a day is plenty for most shops. Twice a day if you like a morning and evening check-in. Pick the times that suit you.",
      },
      {
        target: "[data-testid='rundown-save']",
        title: "Save, and I'll show up",
        text: "Save, and your rundown appears in the notifications right here in the app at your times. Nothing is sent to your phone or email.",
      },
    ],
  },
};

export const TOUR_IDS = Object.keys(TOURS);

/**
 * The FULL walkthrough is the five topics back to back, in the order a
 * new user meets them. Completing it (and only it) is what the backend
 * records for the proactive-offer suppression window.
 */
export const FULL_TOUR_ID = "full-walkthrough";
const FULL_TOUR = {
  title: "The full walkthrough",
  steps: TOUR_IDS.flatMap((id) =>
    TOURS[id].steps.map((s) => ({
      ...s, section: TOURS[id].title, tab: TOURS[id].tab,
    }))),
};

export const ALL_TOURS = { ...TOURS, [FULL_TOUR_ID]: FULL_TOUR };

/** Pure tour state. `active: false` is the only state on page load. */
export const initialTourState = { active: false, tourId: null, step: 0 };

export function tourReducer(state, action) {
  switch (action.type) {
    case "start":
      return ALL_TOURS[action.tourId]
        ? { active: true, tourId: action.tourId, step: 0 }
        : state;
    case "next": {
      if (!state.active) return state;
      const last = ALL_TOURS[state.tourId].steps.length - 1;
      return state.step >= last ? initialTourState : { ...state, step: state.step + 1 };
    }
    case "back":
      return state.active ? { ...state, step: Math.max(0, state.step - 1) } : state;
    case "exit":
      return initialTourState;
    default:
      return state;
  }
}

/** Tours are on-demand only — this is the single source of that truth. */
export function toursAutoStart() {
  return false;
}
