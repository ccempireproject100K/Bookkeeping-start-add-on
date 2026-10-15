import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { FolderOpen, Mic, MicOff, Music2, Pause, Play, Radio, SkipBack, SkipForward, Volume1, Volume2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Track { title: string; src: string; live?: boolean }

// Free, listener-supported public streams (no keys, no accounts).
const STATIONS: Track[] = [
  { title: "SomaFM · Groove Salad (chill)", src: "https://ice1.somafm.com/groovesalad-128-mp3", live: true },
  { title: "SomaFM · Lush (vocals)", src: "https://ice1.somafm.com/lush-128-mp3", live: true },
  { title: "SomaFM · Underground 80s", src: "https://ice1.somafm.com/u80s-128-mp3", live: true },
  { title: "Radio Paradise · Main mix", src: "https://stream.radioparadise.com/mp3-128", live: true },
  { title: "KEXP 90.3 Seattle", src: "https://kexp-mp3-128.streamguys1.com/kexp128.mp3", live: true },
];

// Minimal Web Speech API typing (not in TS's DOM lib).
export interface SpeechRes { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>; resultIndex: number }
export interface Recognizer { continuous: boolean; interimResults: boolean; lang: string; start(): void; stop(): void; onresult: ((e: SpeechRes) => void) | null; onend: (() => void) | null; onerror: ((e: { error: string }) => void) | null }
type RecCtor = new () => Recognizer;
export const getRec = (): RecCtor | undefined => (window as unknown as { SpeechRecognition?: RecCtor; webkitSpeechRecognition?: RecCtor }).SpeechRecognition
  ?? (window as unknown as { webkitSpeechRecognition?: RecCtor }).webkitSpeechRecognition;

/** Floating music player: your own files or free internet radio, with hands-free voice commands. */
export default function MusicPlayer() {
  const audio = useRef<HTMLAudioElement>(new Audio());
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState<"files" | "radio">("radio");
  const [files, setFiles] = useState<Track[]>([]);
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [vol, setVol] = useState(0.6);
  const [listening, setListening] = useState(false);
  const [heard, setHeard] = useState("");
  const rec = useRef<Recognizer | null>(null);
  const wantListen = useRef(false);
  const list = source === "radio" ? STATIONS : files;
  const track = list[idx];

  const load = useCallback((t: Track | undefined, autoplay: boolean) => {
    const a = audio.current;
    if (!t) return;
    if (a.src !== t.src) a.src = t.src;
    if (autoplay) a.play().then(() => setPlaying(true)).catch(() => { setPlaying(false); toast.error("Couldn't play that — try another station or file"); });
  }, []);

  useEffect(() => { audio.current.volume = vol; }, [vol]);
  useEffect(() => {
    const a = audio.current;
    const onEnd = () => setIdx((i) => (list.length ? (i + 1) % list.length : 0));
    const onPause = () => setPlaying(false);
    const onPlay = () => setPlaying(true);
    a.addEventListener("ended", onEnd); a.addEventListener("pause", onPause); a.addEventListener("play", onPlay);
    return () => { a.removeEventListener("ended", onEnd); a.removeEventListener("pause", onPause); a.removeEventListener("play", onPlay); };
  }, [list.length]);
  useEffect(() => { if (playing) load(track, true); /* track change while playing */ }, [idx, source]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { audio.current.pause(); files.forEach((f) => URL.revokeObjectURL(f.src)); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const play = () => load(track, true);
  const pause = () => audio.current.pause();
  const next = () => setIdx((i) => (list.length ? (i + 1) % list.length : 0));
  const prev = () => setIdx((i) => (list.length ? (i - 1 + list.length) % list.length : 0));
  const louder = () => setVol((v) => Math.min(1, +(v + 0.15).toFixed(2)));
  const quieter = () => setVol((v) => Math.max(0, +(v - 0.15).toFixed(2)));

  const command = useCallback((raw: string) => {
    const t = raw.toLowerCase();
    setHeard(raw.trim());
    if (/\b(pause|stop)\b/.test(t)) pause();
    else if (/\b(next|skip)\b/.test(t)) { next(); if (!playing) play(); }
    else if (/\b(previous|back)\b/.test(t)) prev();
    else if (/\b(louder|volume up|turn it up)\b/.test(t)) louder();
    else if (/\b(quieter|softer|volume down|turn it down)\b/.test(t)) quieter();
    else if (/\b(radio|station)\b/.test(t)) { setSource("radio"); setIdx(0); setTimeout(() => load(STATIONS[0], true), 50); }
    else if (/\b(my music|my files|playlist)\b/.test(t)) { if (files.length) { setSource("files"); setIdx(0); setTimeout(() => load(files[0], true), 50); } }
    else if (/\b(play|resume|start)\b/.test(t)) play();
  }, [playing, files, track]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleVoice = () => {
    const Ctor = getRec();
    if (!Ctor) { toast.error("Voice commands need Chrome, Edge or Safari"); return; }
    if (listening) { wantListen.current = false; rec.current?.stop(); setListening(false); return; }
    const r = new Ctor();
    r.continuous = true; r.interimResults = false; r.lang = navigator.language || "en-US";
    r.onresult = (e) => { for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) command(e.results[i][0].transcript); };
    r.onerror = (e) => { if (e.error === "not-allowed") { wantListen.current = false; setListening(false); toast.error("Microphone permission denied"); } };
    r.onend = () => { if (wantListen.current) { try { r.start(); } catch { /* restarting */ } } };
    rec.current = r; wantListen.current = true;
    r.start(); setListening(true);
    toast.info('Say "play", "pause", "next", "louder", "quieter", "radio" or "my music"');
  };
  useEffect(() => { if (rec.current) rec.current.onresult = (e) => { for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) command(e.results[i][0].transcript); }; }, [command]);
  useEffect(() => () => { wantListen.current = false; rec.current?.stop(); }, []);

  const pick = (fl: FileList | null) => {
    if (!fl?.length) return;
    const ts = [...fl].filter((f) => f.type.startsWith("audio/") || /\.(mp3|m4a|aac|ogg|wav|flac)$/i.test(f.name)).map((f) => ({ title: f.name.replace(/\.[^.]+$/, ""), src: URL.createObjectURL(f) }));
    setFiles((prevFiles) => [...prevFiles, ...ts]);
    setSource("files"); setIdx(files.length);
    setTimeout(() => load(ts[0], true), 50);
  };

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} data-testid="music-open-button" aria-label="Open music player"
        className={cn("fixed bottom-4 right-4 z-30 grid size-12 place-items-center rounded-full bg-primary text-primary-foreground shadow-lg transition-transform duration-150 hover:scale-105 print:hidden", playing && "animate-pulse-dot")}>
        <Music2 className="size-5" />
      </button>
    );
  }

  return (
    <div className="fixed inset-x-3 bottom-3 z-30 rounded-xl border border-border bg-card/95 p-3 shadow-2xl backdrop-blur-xl sm:left-auto sm:right-4 sm:w-80 print:hidden" data-testid="music-player">
      <div className="flex items-center justify-between">
        <div className="flex gap-1 rounded-md bg-muted p-0.5">
          {(["radio", "files"] as const).map((s) => (
            <button key={s} onClick={() => { setSource(s); setIdx(0); }} data-testid={`music-source-${s}`}
              className={cn("flex items-center gap-1 rounded px-2 py-1 text-xs font-medium transition-colors duration-150", source === s ? "bg-card shadow-sm" : "text-muted-foreground")}>
              {s === "radio" ? <><Radio className="size-3" /> Radio</> : <><FolderOpen className="size-3" /> My music</>}
            </button>
          ))}
        </div>
        <Button variant="ghost" size="icon-xs" onClick={() => setOpen(false)} data-testid="music-minimize-button" aria-label="Minimise"><X /></Button>
      </div>

      <div className="mt-3 min-h-10">
        <div className="truncate text-sm font-medium" data-testid="music-now-playing">{track?.title ?? (source === "files" ? "Pick some audio files" : "—")}</div>
        <div className="text-[11px] text-muted-foreground">{track?.live ? "Live stream" : source === "files" ? `${files.length} file(s) · stays on this device` : ""}{playing ? " · playing" : ""}</div>
      </div>

      <div className="mt-2 flex items-center justify-between">
        <Button variant="ghost" size="icon-sm" onClick={prev} data-testid="music-prev-button"><SkipBack /></Button>
        <Button size="icon" className="rounded-full" onClick={() => (playing ? pause() : play())} disabled={!track} data-testid="music-play-button">{playing ? <Pause /> : <Play />}</Button>
        <Button variant="ghost" size="icon-sm" onClick={next} data-testid="music-next-button"><SkipForward /></Button>
        <Button variant="ghost" size="icon-sm" onClick={quieter} data-testid="music-quieter-button"><Volume1 /></Button>
        <input type="range" min={0} max={1} step={0.05} value={vol} onChange={(e) => setVol(Number(e.target.value))} className="w-16 accent-[var(--primary)]" data-testid="music-volume-slider" aria-label="Volume" />
        <Button variant="ghost" size="icon-sm" onClick={louder} data-testid="music-louder-button"><Volume2 /></Button>
      </div>

      <div className="mt-2 flex items-center gap-2">
        <Button variant={listening ? "default" : "outline"} size="sm" className="flex-1" onClick={toggleVoice} data-testid="music-voice-button">
          {listening ? <><Mic className="animate-pulse-dot" /> Listening…</> : <><MicOff /> Voice control</>}
        </Button>
        {source === "files" && (
          <label className="cursor-pointer">
            <input type="file" accept="audio/*" multiple className="sr-only" onChange={(e) => pick(e.target.files)} data-testid="music-file-input" />
            <span className="inline-flex h-8 items-center gap-1 rounded-md border border-border px-2.5 text-sm hover:bg-muted"><FolderOpen className="size-4" /> Add</span>
          </label>
        )}
      </div>
      {heard && <div className="mt-1 truncate text-[11px] text-muted-foreground" data-testid="music-heard">Heard: "{heard}"</div>}

      {list.length > 1 && (
        <div className="mt-2 max-h-28 space-y-0.5 overflow-y-auto border-t border-border pt-2">
          {list.map((t, i) => (
            <button key={t.src} onClick={() => { setIdx(i); load(t, true); }} data-testid={`music-track-${i}`}
              className={cn("block w-full truncate rounded px-2 py-1 text-left text-xs transition-colors duration-150 hover:bg-muted", i === idx && "bg-primary/10 font-medium")}>{t.title}</button>
          ))}
        </div>
      )}
    </div>
  );
}
