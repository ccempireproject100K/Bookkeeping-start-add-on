import { useEffect, useRef, useState } from "react";
import { Html5Qrcode, Html5QrcodeSupportedFormats } from "html5-qrcode";
import { Camera, CameraOff, Keyboard } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const FORMATS = [
  Html5QrcodeSupportedFormats.EAN_13, Html5QrcodeSupportedFormats.EAN_8, Html5QrcodeSupportedFormats.UPC_A,
  Html5QrcodeSupportedFormats.UPC_E, Html5QrcodeSupportedFormats.CODE_128, Html5QrcodeSupportedFormats.CODE_39,
  Html5QrcodeSupportedFormats.QR_CODE,
];

/**
 * Camera scanner (open-source html5-qrcode / ZXing, native BarcodeDetector when the browser has it)
 * plus a typed fallback that also catches USB/Bluetooth keyboard-wedge scanners (they type + Enter).
 */
export default function Scanner({ onCode, autoStart = true }: { onCode: (code: string) => void; autoStart?: boolean }) {
  const ref = useRef<Html5Qrcode | null>(null);
  const done = useRef(false);
  const [on, setOn] = useState(false);
  const [err, setErr] = useState("");
  const [typed, setTyped] = useState("");
  const id = "barcode-camera-region";

  const stop = async () => {
    const s = ref.current;
    ref.current = null;
    setOn(false);
    if (s) {
      try { if (s.isScanning) await s.stop(); s.clear(); } catch { /* already stopped */ }
    }
  };

  const start = async () => {
    setErr("");
    done.current = false;
    try {
      const s = new Html5Qrcode(id, { formatsToSupport: FORMATS, verbose: false, experimentalFeatures: { useBarCodeDetectorIfSupported: true } });
      ref.current = s;
      await s.start({ facingMode: "environment" }, { fps: 15, qrbox: (w: number, h: number) => ({ width: Math.min(300, w * 0.85), height: Math.min(160, h * 0.6) }) },
        (text) => {
          if (done.current) return;
          done.current = true;
          navigator.vibrate?.(60);
          void stop();
          onCode(text.trim());
        }, () => undefined);
      setOn(true);
    } catch (e) {
      ref.current = null;
      setErr(e instanceof Error ? e.message : "Camera unavailable — type or use a hardware scanner below.");
    }
  };

  useEffect(() => {
    if (autoStart) void start();
    return () => { void stop(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-3">
      <div className="relative overflow-hidden rounded-lg border border-border bg-black/90 aspect-[4/3]">
        <div id={id} className="h-full w-full [&_video]:h-full [&_video]:w-full [&_video]:object-cover" data-testid="scanner-camera-region" />
        {!on && (
          <div className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-white/70">
            <div>
              <CameraOff className="mx-auto mb-2 size-6" />
              {err || "Camera is off"}
            </div>
          </div>
        )}
        {on && <div className="pointer-events-none absolute inset-x-8 top-1/2 h-px bg-red-500/80 shadow-[0_0_12px_2px_rgba(239,68,68,0.6)] animate-scanline" />}
      </div>
      <div className="flex gap-2">
        {on ? (
          <Button type="button" variant="outline" size="sm" onClick={() => void stop()} data-testid="scanner-stop-button"><CameraOff /> Stop camera</Button>
        ) : (
          <Button type="button" variant="outline" size="sm" onClick={() => void start()} data-testid="scanner-start-button"><Camera /> Start camera</Button>
        )}
      </div>
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (typed.trim()) { onCode(typed.trim()); setTyped(""); } }}>
        <div className="relative flex-1">
          <Keyboard className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input autoFocus={!autoStart} value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Type or scan barcode / SKU"
            className="pl-8 font-mono" inputMode="numeric" data-testid="scanner-manual-input" />
        </div>
        <Button type="submit" data-testid="scanner-manual-submit">Look up</Button>
      </form>
    </div>
  );
}
