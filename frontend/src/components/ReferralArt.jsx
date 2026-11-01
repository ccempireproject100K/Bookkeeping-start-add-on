import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { nicheIdFromFace } from "@/lib/packs";

// Niche artwork for the referral page. The artwork follows the NICHE (the
// workspace's edition face), never the market: the same art shows on every
// market face of that niche. The pieces live in src/referral-art/<slug>.js
// as base64 text — the repo's Git channel cannot carry binaries, so, like
// Trade Circle's server-side illustrations, the art travels as text and is
// decoded here at render time. Each niche's module is its own webpack
// chunk, loaded only when that niche's page renders. A generic face (no
// niche), an unknown niche, or a failed decode renders nothing.

// One lazy loader per niche (each is its own webpack chunk).
const LOADERS = {
  appliance_repair: () => import("@/referral-art/appliance_repair"),
  auto_repair: () => import("@/referral-art/auto_repair"),
  barbershop: () => import("@/referral-art/barbershop"),
  cleaning: () => import("@/referral-art/cleaning"),
  credit_repair: () => import("@/referral-art/credit_repair"),
  decks_outdoor: () => import("@/referral-art/decks_outdoor"),
  electrical: () => import("@/referral-art/electrical"),
  fencing: () => import("@/referral-art/fencing"),
  flooring: () => import("@/referral-art/flooring"),
  foundation_repair: () => import("@/referral-art/foundation_repair"),
  freelancer: () => import("@/referral-art/freelancer"),
  garage_door: () => import("@/referral-art/garage_door"),
  gig_work: () => import("@/referral-art/gig_work"),
  handyman: () => import("@/referral-art/handyman"),
  home_inspection: () => import("@/referral-art/home_inspection"),
  hvac: () => import("@/referral-art/hvac"),
  irrigation: () => import("@/referral-art/irrigation"),
  junk_removal: () => import("@/referral-art/junk_removal"),
  kitchen_bath: () => import("@/referral-art/kitchen_bath"),
  landscaping: () => import("@/referral-art/landscaping"),
  locksmith: () => import("@/referral-art/locksmith"),
  painting: () => import("@/referral-art/painting"),
  pest_control: () => import("@/referral-art/pest_control"),
  plumbing: () => import("@/referral-art/plumbing"),
  pool_service: () => import("@/referral-art/pool_service"),
  real_estate: () => import("@/referral-art/real_estate"),
  restoration: () => import("@/referral-art/restoration"),
  roofing: () => import("@/referral-art/roofing"),
  septic_well: () => import("@/referral-art/septic_well"),
  siding_gutters: () => import("@/referral-art/siding_gutters"),
  small_business_owner: () => import("@/referral-art/small_business_owner"),
  smart_home: () => import("@/referral-art/smart_home"),
  solar: () => import("@/referral-art/solar"),
  tree_service: () => import("@/referral-art/tree_service"),
  whole_home_generators: () => import("@/referral-art/whole_home_generators"),
  windows_doors: () => import("@/referral-art/windows_doors"),
};

function objectUrlFromBase64(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return URL.createObjectURL(new Blob([bytes], { type: "image/webp" }));
}

export default function ReferralArt({ pack }) {
  const { t } = useTranslation();
  const slug = nicheIdFromFace(pack);
  const [src, setSrc] = useState(null);

  useEffect(() => {
    if (!slug) {
      setSrc(null);
      return undefined;
    }
    const load = LOADERS[slug];
    if (!load) {
      setSrc(null);
      return undefined;
    }
    let live = true;
    let url = null;
    load()
      .then((mod) => {
        if (!live) return;
        url = objectUrlFromBase64(mod.default);
        setSrc(url);
      })
      .catch(() => {
        if (live) setSrc(null);
      });
    return () => {
      live = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [slug]);

  if (!slug || !src) return null;
  return (
    <img
      src={src}
      alt={t("referral.artAlt", "Illustration of the {{trade}} trade at work", {
        trade: pack?.trade || slug.replace(/_/g, " "),
      })}
      width="1200"
      height="800"
      className="mb-6 w-full rounded-2xl border border-[#232936] object-cover"
      style={{ maxHeight: 300, objectPosition: "center 30%" }}
    />
  );
}
