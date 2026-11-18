import React from "react";
import { ExternalLink, LifeBuoy, PlayCircle, Search } from "lucide-react";
import { HELP_LABEL, VIDEO_LABEL, groupHelpLinks, groupHelpVideos } from "../lib/helpLinks";

/**
 * "Free help & learning" — curated OFFICIAL free public resources,
 * grouped by topic, each a plain outbound link with a one-line
 * plain-English description. Config-driven (lib/helpLinks.json).
 * tone: "dark" (app surfaces) or "light" (client page light mode).
 */
export default function FreeHelpPanel({ tone = "dark" }) {
  const groups = groupHelpLinks();
  const videoGroups = groupHelpVideos();
  const dark = tone !== "light";
  const head = dark ? "text-gray-100" : "text-gray-900";
  const sub = dark ? "text-gray-400" : "text-gray-600";
  const topicCls = dark ? "text-amber-400" : "text-amber-700";
  const linkCls = dark
    ? "border-[#222838] bg-[#14171E] hover:border-[#333c52]"
    : "border-[#D3D8E2] bg-white hover:border-gray-400";
  const nameCls = dark ? "text-gray-100" : "text-gray-900";
  return (
    <div data-testid="free-help-panel">
      <div className="flex items-center gap-2">
        <LifeBuoy size={16} className={dark ? "text-amber-400" : "text-amber-600"} />
        <h4 className={`font-display font-semibold ${head}`}>Free help &amp; learning</h4>
      </div>
      <p className={`mt-1 text-xs ${sub}`} data-testid="free-help-label">{HELP_LABEL}</p>
      <div className="mt-3 space-y-4">
        {groups.map((g) => (
          <div key={g.id} data-testid={`free-help-topic-${g.id}`}>
            <p className={`text-[11px] font-semibold uppercase tracking-wider ${topicCls}`}>{g.label}</p>
            <div className="mt-1.5 space-y-1.5">
              {g.links.map((l) => (
                <a key={l.url} href={l.url} target="_blank" rel="noreferrer"
                  data-testid={`free-help-link-${l.name}`}
                  className={`block rounded-lg border p-3 transition-colors ${linkCls}`}>
                  <span className={`flex items-center gap-1.5 text-sm font-medium ${nameCls}`}>
                    {l.name} <ExternalLink size={13} className="shrink-0 opacity-60" />
                  </span>
                  <span className={`mt-0.5 block text-xs ${sub}`}>{l.blurb}</span>
                </a>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* Watch a human walk through it — short YouTube walkthroughs,
          one specific question each. Links out; nothing embedded. */}
      <div className="mt-5" data-testid="free-help-videos">
        <div className="flex items-center gap-2">
          <PlayCircle size={16} className={dark ? "text-amber-400" : "text-amber-600"} />
          <h5 className={`font-display text-sm font-semibold ${head}`}>Watch a human walk through it</h5>
        </div>
        <p className={`mt-1 text-xs ${sub}`} data-testid="free-help-video-label">{VIDEO_LABEL}</p>
        <div className="mt-3 space-y-4">
          {videoGroups.map((g) => (
            <div key={g.id} data-testid={`free-help-video-topic-${g.id}`}>
              <p className={`text-[11px] font-semibold uppercase tracking-wider ${topicCls}`}>{g.label}</p>
              <div className="mt-1.5 space-y-1.5">
                {g.videos.map((v) => (
                  <a key={v.url} href={v.url} target="_blank" rel="noreferrer"
                    data-testid={`free-help-video-${v.title}`}
                    className={`block rounded-lg border p-3 transition-colors ${linkCls}`}>
                    <span className={`flex items-center gap-1.5 text-sm font-medium ${nameCls}`}>
                      {v.kind === "search"
                        ? <Search size={14} className="shrink-0 opacity-70" />
                        : <PlayCircle size={14} className="shrink-0 opacity-70" />}
                      {v.title}
                    </span>
                    <span className={`mt-0.5 block text-xs ${sub}`}>{v.blurb}</span>
                    <span className={`mt-0.5 block text-[11px] ${sub}`}>{v.source}</span>
                  </a>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
