/**
 * GuideCharacter — the guide's avatar slot.
 *
 * A swappable presentation layer over the conversation: it renders
 * whatever skin the slot config names (lib/guide.js GUIDE_CHARACTER)
 * in one of the shared rig's states (idle / walk / errand-run /
 * talking / celebrate / error). Bookkeeping's skin is the bear —
 * full body, vest and pants, glasses (owner ruling 2026-10-31).
 *
 * NO final artwork ships here: the bear renders as a labeled
 * placeholder silhouette (simple shapes standing in for the art —
 * ears, snout, vest, pants, glasses) until the owner's visual ruling
 * produces real artwork, which then replaces the shapes in this one
 * component via the skin config.
 */
import React from 'react';
import { GUIDE_CHARACTER, stateClass } from '@/lib/guide';

const CSS = `
@keyframes gc-bob { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-4px); } }
@keyframes gc-slide { 0% { transform: translateX(-14px); } 50% { transform: translateX(14px); } 100% { transform: translateX(-14px); } }
@keyframes gc-run { 0% { transform: translateX(-30px); } 50% { transform: translateX(30px); } 100% { transform: translateX(-30px); } }
@keyframes gc-pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.05); } }
@keyframes gc-bounce { 0%,100% { transform: translateY(0); } 30% { transform: translateY(-14px); } 60% { transform: translateY(0); } }
@keyframes gc-shake { 0%,100% { transform: translateX(0); } 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); } }
@keyframes gc-dust { 0% { opacity: 0; transform: translateX(0) scale(.6); } 30% { opacity: .8; } 100% { opacity: 0; transform: translateX(-22px) scale(1.3); } }
.gc-idle { animation: gc-bob 3s ease-in-out infinite; }
.gc-walk { animation: gc-slide 2.4s ease-in-out infinite; }
.gc-errand-run { animation: gc-run .8s ease-in-out infinite; }
.gc-talking { animation: gc-pulse 1.2s ease-in-out infinite; }
.gc-celebrate { animation: gc-bounce .9s ease-in-out infinite; }
.gc-error { animation: gc-shake .5s ease-in-out infinite; }
.gc-dust { animation: gc-dust .8s linear infinite; }
`;

function BearSilhouette() {
  // Placeholder shapes ONLY — a stand-in silhouette, not artwork.
  const fur = '#8a7360';
  const dark = '#4c3f35';
  return (
    <svg viewBox="0 0 120 150" width="84" height="105" role="img"
      aria-label="Guide character placeholder — a bear in a vest and pants, wearing glasses">
      <circle cx="32" cy="22" r="11" fill={fur} />
      <circle cx="88" cy="22" r="11" fill={fur} />
      <ellipse cx="60" cy="38" rx="26" ry="22" fill={fur} />
      <ellipse cx="60" cy="46" rx="11" ry="8" fill="#c9b6a4" />
      <circle cx="60" cy="42" r="3.5" fill={dark} />
      {/* glasses */}
      <g stroke={dark} strokeWidth="2.5" fill="none">
        <circle cx="49" cy="34" r="8" />
        <circle cx="71" cy="34" r="8" />
        <line x1="57" y1="34" x2="63" y2="34" />
      </g>
      {/* body + vest */}
      <rect x="34" y="60" width="52" height="46" rx="14" fill={fur} />
      <path d="M34 66 L52 60 L52 106 L34 100 Z" fill={dark} opacity="0.75" />
      <path d="M86 66 L68 60 L68 106 L86 100 Z" fill={dark} opacity="0.75" />
      {/* pants + legs */}
      <rect x="38" y="104" width="20" height="30" rx="6" fill="#3d4b63" />
      <rect x="62" y="104" width="20" height="30" rx="6" fill="#3d4b63" />
      <rect x="36" y="132" width="24" height="7" rx="3.5" fill={dark} />
      <rect x="60" y="132" width="24" height="7" rx="3.5" fill={dark} />
    </svg>
  );
}

export default function GuideCharacter({ state = 'idle', character = GUIDE_CHARACTER }) {
  const cls = stateClass(state);
  return (
    <div className="flex flex-col items-center select-none" data-testid="guide-character"
      data-state={state} data-skin={character.skin}>
      <style>{CSS}</style>
      <div className="relative h-[110px] flex items-end">
        {state === 'errand-run' && (
          <span className="gc-dust absolute bottom-1 left-1 flex gap-1" aria-hidden="true">
            <span className="w-2.5 h-2.5 rounded-full bg-[#c9b6a4] inline-block" />
            <span className="w-2 h-2 rounded-full bg-[#c9b6a4] inline-block" />
            <span className="w-1.5 h-1.5 rounded-full bg-[#c9b6a4] inline-block" />
          </span>
        )}
        <div className={cls}>
          <BearSilhouette />
        </div>
      </div>
      <span className="text-[10px] text-gray-500 mt-1">
        {character.name} · placeholder art — the real bear is still being drawn
      </span>
    </div>
  );
}
