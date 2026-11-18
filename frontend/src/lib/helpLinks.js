/**
 * Free public help links (owner addition 2026-11-14, the answer to gap
 * #13's chatbot-only support): when a money/business question goes beyond
 * our product, we point people at OFFICIAL free public resources instead
 * of leaving them with no real help.
 *
 * The link list is CONFIG, not code: it lives in helpLinks.json (name,
 * url, topic, blurb) and can be edited without touching any logic.
 * Outbound links only — we host nothing of theirs; cost is $0.
 * Every URL in the shipped config was verified to resolve on 2026-11-14.
 */
import config from "./helpLinks.json";

export const HELP_TOPICS = config.topics;
export const HELP_LINKS = config.links;

/** The honest label, shown verbatim wherever the panel appears. */
export const HELP_LABEL =
  "Free public resources. These aren't ours and this isn't financial " +
  "advice — they're the official places to learn.";

/** Group links under their topics, in config topic order. Pure. */
export function groupHelpLinks(links = HELP_LINKS, topics = HELP_TOPICS) {
  return topics
    .map((t) => ({
      ...t,
      links: links.filter((l) => l.topic === t.id),
    }))
    .filter((g) => g.links.length > 0);
}

/**
 * "Watch a human walk through it" — short YouTube walkthroughs where a
 * real person answers one specific question (owner addition 2026-11-14,
 * curated to 6–9 entries total). Same config file; kind "video" is a
 * specific verified video, kind "search" is a labeled YouTube search
 * ("More videos on…") used where no single video verified cleanly.
 * Links out only — nothing embedded, page stays light.
 */
export const HELP_VIDEO_TOPICS = config.videoTopics || [];
export const HELP_VIDEOS = config.videos || [];

export const VIDEO_LABEL =
  "Free public videos on YouTube. These aren't ours either — just " +
  "people walking through it, for folks who learn by watching.";

export function isYouTubeUrl(url) {
  return (
    typeof url === "string" &&
    url.startsWith("https://") &&
    (url.includes("youtube.com/watch") || url.includes("youtube.com/results"))
  );
}

/** Group videos under their topics, in config topic order. Pure. */
export function groupHelpVideos(videos = HELP_VIDEOS, topics = HELP_VIDEO_TOPICS) {
  return topics
    .map((t) => ({
      ...t,
      videos: videos.filter((v) => v.topic === t.id),
    }))
    .filter((g) => g.videos.length > 0);
}
