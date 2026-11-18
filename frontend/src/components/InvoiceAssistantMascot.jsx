import React from "react";

/**
 * Invoice Assistant mascot — an original, code-drawn cash register monster.
 * It "eats invoices": receipt paper curls out of its head, its mouth is the
 * register's card slot, its belly is a keypad, and its cash drawer pops
 * open when money lands. Pure SVG + CSS animation — no external assets.
 *
 * mood: "idle" (gentle bob) | "eating" (chomping, drawer working — new
 * invoices issued) | "happy" (drawer open, check badge — payment landed).
 * theme: "dark" | "light" — character colors hold in both; only the
 * outline/shadow register changes. Name is intentionally NOT here: the
 * owner hasn't named it; UI text labels it "Invoice Assistant".
 */
export default function InvoiceAssistantMascot({ mood = "idle", theme = "dark", size = 96, className = "" }) {
  const eating = mood === "eating";
  const happy = mood === "happy";
  const stroke = theme === "light" ? "#92400E" : "#78350F";
  return (
    <svg
      viewBox="0 0 200 196" width={size} height={size * 0.98}
      className={`${className} ${eating || happy ? "" : "mascot-bob"}`}
      role="img" aria-label="Invoice Assistant — cash register monster"
      data-testid="assistant-mascot" data-mood={mood}
    >
      {/* receipt paper curling out of the top */}
      <g className={eating ? "mascot-paper" : ""}>
        <path d="M70 6 h60 v26 c-6 6 -14 -4 -20 2 c-6 5 -14 -5 -20 -1 c-7 4 -14 -3 -20 1 z"
          fill="#FFFFFF" stroke={stroke} strokeWidth="3" strokeLinejoin="round" />
        <line x1="80" y1="15" x2="120" y2="15" stroke="#D1D5DB" strokeWidth="4" strokeLinecap="round" />
        <line x1="84" y1="24" x2="116" y2="24" stroke="#D1D5DB" strokeWidth="4" strokeLinecap="round" />
      </g>

      {/* arms */}
      <path d="M28 108 q-16 8 -10 24" fill="none" stroke="#D97706" strokeWidth="10" strokeLinecap="round" />
      <path d="M172 108 q16 8 10 24" fill="none" stroke="#D97706" strokeWidth="10" strokeLinecap="round" />

      {/* body — the register */}
      <rect x="34" y="36" width="132" height="118" rx="20"
        fill="#F59E0B" stroke={stroke} strokeWidth="3" />

      {/* display screen with the eyes */}
      <rect x="52" y="48" width="96" height="42" rx="10"
        fill={theme === "light" ? "#1F2937" : "#111827"} stroke={stroke} strokeWidth="2" />
      {happy ? (
        <>
          <path d="M68 72 q10 -12 20 0" fill="none" stroke="#A7F3D0" strokeWidth="5" strokeLinecap="round" />
          <path d="M112 72 q10 -12 20 0" fill="none" stroke="#A7F3D0" strokeWidth="5" strokeLinecap="round" />
        </>
      ) : (
        <>
          <circle cx="78" cy="66" r="10" fill="#FFFFFF" />
          <circle cx="122" cy="66" r="10" fill="#FFFFFF" />
          <circle cx={eating ? 80 : 78} cy="68" r="5" fill="#111827" />
          <circle cx={eating ? 124 : 122} cy="68" r="5" fill="#111827" />
        </>
      )}

      {/* mouth — the card slot, with keypad teeth */}
      <g className={eating ? "mascot-chomp" : ""} style={{ transformOrigin: "100px 104px" }}>
        <rect x="62" y="98" width="76" height="14" rx="7"
          fill="#7C2D12" stroke={stroke} strokeWidth="2" />
        <rect x="70" y="99" width="8" height="6" rx="1.5" fill="#FEF3C7" />
        <rect x="84" y="99" width="8" height="6" rx="1.5" fill="#FEF3C7" />
        <rect x="98" y="99" width="8" height="6" rx="1.5" fill="#FEF3C7" />
        <rect x="112" y="99" width="8" height="6" rx="1.5" fill="#FEF3C7" />
      </g>

      {/* belly keypad */}
      <rect x="66" y="120" width="16" height="10" rx="3" fill="#FDE68A" stroke={stroke} strokeWidth="1.5" />
      <rect x="92" y="120" width="16" height="10" rx="3" fill="#10B981" stroke={stroke} strokeWidth="1.5" />
      <rect x="118" y="120" width="16" height="10" rx="3" fill="#FDE68A" stroke={stroke} strokeWidth="1.5" />
      <circle cx="74" cy="140" r="5" fill="#FDE68A" stroke={stroke} strokeWidth="1.5" />
      <circle cx="100" cy="140" r="5" fill="#FDE68A" stroke={stroke} strokeWidth="1.5" />
      <circle cx="126" cy="140" r="5" fill="#10B981" stroke={stroke} strokeWidth="1.5" />

      {/* cash drawer — pops open when happy/eating */}
      <g className={happy || eating ? "mascot-drawer-open" : ""}>
        <rect x="48" y="152" width="104" height="18" rx="6"
          fill="#D97706" stroke={stroke} strokeWidth="2.5" />
        <rect x="88" y="158" width="24" height="5" rx="2.5" fill="#FEF3C7" />
        {(happy || eating) && (
          <>
            <circle cx="72" cy="150" r="7" fill="#FBBF24" stroke={stroke} strokeWidth="2" />
            <circle cx="128" cy="150" r="7" fill="#FBBF24" stroke={stroke} strokeWidth="2" />
            <text x="72" y="154" textAnchor="middle" fontSize="9" fontWeight="700" fill="#78350F">$</text>
            <text x="128" y="154" textAnchor="middle" fontSize="9" fontWeight="700" fill="#78350F">$</text>
          </>
        )}
      </g>

      {/* feet */}
      <ellipse cx="66" cy="182" rx="16" ry="6" fill="#D97706" stroke={stroke} strokeWidth="2" />
      <ellipse cx="134" cy="182" rx="16" ry="6" fill="#D97706" stroke={stroke} strokeWidth="2" />

      {/* paid check badge */}
      {happy && (
        <g className="mascot-pop">
          <circle cx="168" cy="40" r="16" fill="#10B981" stroke="#065F46" strokeWidth="3" />
          <path d="M159 40 l7 7 l11 -13" fill="none" stroke="#FFFFFF" strokeWidth="4.5"
            strokeLinecap="round" strokeLinejoin="round" />
        </g>
      )}
    </svg>
  );
}
