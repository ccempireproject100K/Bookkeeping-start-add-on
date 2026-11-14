import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Camera, X } from "lucide-react";

/**
 * Scan-to-move input: type/paste a code, or scan it with the device
 * camera where the browser offers one (BarcodeDetector). Manual entry
 * is ALWAYS present — the camera is a convenience, never a requirement.
 *
 * The value it produces is just the code string; callers post it to
 * /inventory/scan. When the app is offline, the same payload can ride
 * the #13 field-capture queue as kind "stock_scan" with the queue
 * item's id as client_key (the scan endpoint is idempotent on it).
 */
export default function StockScanInput({ value, onChange, placeholder = "Serial or lot code", testid }) {
  const [scanning, setScanning] = useState(false);
  const [camNote, setCamNote] = useState("");
  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const timerRef = useRef(null);

  const stop = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (streamRef.current) streamRef.current.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setScanning(false);
  };

  useEffect(() => stop, []);

  const startScan = async () => {
    setCamNote("");
    if (!("BarcodeDetector" in window) || !navigator.mediaDevices?.getUserMedia) {
      setCamNote("Camera scanning isn't available on this device — type the code instead.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
      streamRef.current = stream;
      setScanning(true);
      requestAnimationFrame(async () => {
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play().catch(() => {});
        }
        const detector = new window.BarcodeDetector({ formats: ["qr_code", "code_128", "code_39", "ean_13", "upc_a"] });
        timerRef.current = setInterval(async () => {
          if (!videoRef.current) return;
          try {
            const codes = await detector.detect(videoRef.current);
            if (codes && codes.length) {
              onChange(codes[0].rawValue || "");
              stop();
            }
          } catch { /* keep trying until the user cancels */ }
        }, 400);
      });
    } catch {
      setCamNote("Camera permission was declined — type the code instead.");
      setScanning(false);
    }
  };

  return (
    <div>
      <div className="flex gap-2">
        <Input data-testid={testid} value={value} onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} className="bg-[#0F1218] border-[#222838] font-mono" />
        {scanning
          ? <Button type="button" variant="ghost" onClick={stop} className="text-rose-400 hover:bg-rose-500/10"><X size={15} className="mr-1" /> Stop</Button>
          : <Button type="button" variant="ghost" onClick={startScan} className="text-sky-400 hover:bg-sky-500/10"><Camera size={15} className="mr-1" /> Scan</Button>}
      </div>
      {scanning && <video ref={videoRef} className="mt-2 w-full max-h-56 rounded-lg border border-[#222838] object-cover" muted playsInline />}
      {camNote && <p className="mt-1 text-xs text-amber-300">{camNote}</p>}
    </div>
  );
}
