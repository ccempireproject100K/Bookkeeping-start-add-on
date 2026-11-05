/**
 * PipCharacter — Pip, the file-door runner, as a small inline SVG.
 *
 * Original artwork for this product (DESIGN §5.7): the same teal runner
 * the owner has on his desktop, tinted to the Books palette (amber belly
 * + antenna bead, matching the shell's amber accent) — colour-coordinated,
 * not uniformed. No external assets; every limb animates per pose via
 * pip.css, driven by `data-state`.
 *
 * Poses are WORK states, not decoration (owner scope upgrade 2026-11-05):
 * the shell derives them from the Phase-0 tray state machine — Pip only
 * celebrates on a real adapter DELIVER event, only "carries" while a
 * real upload is in flight, and slumps only on a real failure.
 *
 * @param {{ state: 'walk'|'idle'|'excited'|'thinking'|'carrying'|'celebrate'|'slumped'|'sleep',
 *           facing: 1|-1 }} props
 */
export default function PipCharacter({ state, facing }) {
  const happy = state === "celebrate" || state === "excited";
  const focused = state === "carrying" || state === "thinking";
  const asleep = state === "sleep";
  const slumped = state === "slumped";
  return (
    <div
      className="pip-char"
      data-state={state}
      style={{ transform: `scaleX(${facing})`, width: 76, height: 84 }}
      aria-hidden="true"
    >
      <svg viewBox="0 0 120 112" width="76" height="84">
        {/* shadow */}
        <ellipse cx="60" cy="106" rx="30" ry="4.5" fill="#000000" opacity="0.35" />
        <g className="pip-body-group">
          {/* legs */}
          <g className="pip-leg pip-leg-l">
            <rect x="44" y="80" width="12" height="20" rx="6" fill="#e8532f" />
          </g>
          <g className="pip-leg pip-leg-r">
            <rect x="64" y="80" width="12" height="20" rx="6" fill="#e8532f" />
          </g>
          {/* arms (behind body) */}
          <g className="pip-arm pip-arm-l">
            <rect x="14" y="48" width="11" height="30" rx="5.5" fill="#0d9488" />
          </g>
          <g className="pip-arm pip-arm-r">
            <rect x="95" y="48" width="11" height="30" rx="5.5" fill="#0d9488" />
          </g>
          {/* body — teal is Pip; the amber belly is the Books tint (§5.7) */}
          <ellipse cx="60" cy="56" rx="36" ry="32" fill="#14b8a6" />
          <ellipse cx="60" cy="64" rx="21" ry="18" fill="#f59e0b" opacity="0.92" />
          {/* antenna */}
          <line x1="60" y1="26" x2="60" y2="13" stroke="#0d9488" strokeWidth="4" strokeLinecap="round" />
          <circle cx="60" cy="10" r="5.5" fill="#fbbf24" stroke="#d97706" strokeWidth="1.5" />
          {/* eyes */}
          {asleep ? (
            <>
              <path d="M38 47 Q46 50 54 47" fill="none" stroke="#17322e" strokeWidth="3.5" strokeLinecap="round" />
              <path d="M66 47 Q74 50 82 47" fill="none" stroke="#17322e" strokeWidth="3.5" strokeLinecap="round" />
            </>
          ) : happy ? (
            <>
              <path d="M38 46 Q46 38 54 46" fill="none" stroke="#17322e" strokeWidth="3.5" strokeLinecap="round" />
              <path d="M66 46 Q74 38 82 46" fill="none" stroke="#17322e" strokeWidth="3.5" strokeLinecap="round" />
            </>
          ) : (
            <>
              <circle cx="46" cy="46" r="8.5" fill="#ffffff" />
              <circle cx="74" cy="46" r="8.5" fill="#ffffff" />
              <circle cx={focused ? 46 : 47.5} cy={slumped ? 49 : 47} r="3.8" fill="#17322e" />
              <circle cx={focused ? 74 : 75.5} cy={slumped ? 49 : 47} r="3.8" fill="#17322e" />
            </>
          )}
          {/* cheeks */}
          <circle cx="34" cy="58" r="4.5" fill="#ff9d8a" opacity="0.8" />
          <circle cx="86" cy="58" r="4.5" fill="#ff9d8a" opacity="0.8" />
          {/* mouth */}
          {happy ? (
            <ellipse cx="60" cy="60" rx="6" ry="7" fill="#17322e" />
          ) : focused || slumped || asleep ? (
            <line x1="54" y1="61" x2="66" y2="61" stroke="#17322e" strokeWidth="3" strokeLinecap="round" />
          ) : (
            <path d="M53 59 Q60 65 67 59" fill="none" stroke="#17322e" strokeWidth="3" strokeLinecap="round" />
          )}
          {/* carrying: the file he's hauling to the Receipt Inbox */}
          {state === "carrying" && (
            <g className="pip-carry">
              <rect x="42" y="66" width="36" height="26" rx="3" fill="#f8fafc" stroke="#94a3b8" strokeWidth="2" />
              <line x1="48" y1="74" x2="72" y2="74" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" />
              <line x1="48" y1="81" x2="66" y2="81" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" />
            </g>
          )}
          {/* thinking: the one question, hovering */}
          {state === "thinking" && (
            <g className="pip-think">
              <circle cx="92" cy="20" r="11" fill="#fbbf24" stroke="#d97706" strokeWidth="2" />
              <text x="92" y="25.5" textAnchor="middle" fontSize="14" fontWeight="700" fill="#5b3a00">?</text>
            </g>
          )}
          {/* sleep: a quiet Z */}
          {asleep && (
            <text className="pip-zee" x="90" y="24" fontSize="13" fontWeight="700" fill="#7dd3fc">Z</text>
          )}
        </g>
      </svg>
    </div>
  );
}
