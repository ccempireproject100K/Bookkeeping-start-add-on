import { useEffect, useState } from "react";
import { Download, Share, X } from "lucide-react";

// "Install this app" affordance for the PWA.
//
// Android/Chrome: captures the `beforeinstallprompt` event and offers the
// native install flow. iOS Safari has no such event, so iPhone/iPad users
// get the Share -> "Add to Home Screen" instructions instead (the PWA is
// the iOS door — there is no App Store build, by the owner's ruling).
// Hidden when the app is already running installed (standalone display
// mode), after a successful install, or once dismissed (the dismissal is
// remembered in localStorage so it never nags).

const DISMISS_KEY = "mae-install-dismissed";

function isStandalone() {
  return (
    window.matchMedia?.("(display-mode: standalone)").matches ||
    window.navigator.standalone === true
  );
}

function isIos() {
  return (
    /iphone|ipad|ipod/i.test(window.navigator.userAgent) ||
    (window.navigator.platform === "MacIntel" && window.navigator.maxTouchPoints > 1)
  );
}

export default function InstallPrompt() {
  const [deferredPrompt, setDeferredPrompt] = useState(null);
  const [showIosHint, setShowIosHint] = useState(false);
  const [dismissed, setDismissed] = useState(
    () => window.localStorage.getItem(DISMISS_KEY) === "1",
  );

  useEffect(() => {
    if (isStandalone()) return undefined;

    const onBeforeInstall = (event) => {
      event.preventDefault();
      setDeferredPrompt(event);
    };
    const onInstalled = () => {
      setDeferredPrompt(null);
      setShowIosHint(false);
    };
    window.addEventListener("beforeinstallprompt", onBeforeInstall);
    window.addEventListener("appinstalled", onInstalled);

    if (isIos()) setShowIosHint(true);

    return () => {
      window.removeEventListener("beforeinstallprompt", onBeforeInstall);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  if (dismissed || isStandalone() || (!deferredPrompt && !showIosHint)) {
    return null;
  }

  const dismiss = () => {
    window.localStorage.setItem(DISMISS_KEY, "1");
    setDismissed(true);
  };

  const install = async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    const choice = await deferredPrompt.userChoice;
    if (choice.outcome === "accepted") setDeferredPrompt(null);
    else dismiss();
  };

  return (
    <div
      data-testid="install-prompt"
      className="fixed bottom-4 left-4 right-4 sm:left-auto sm:right-6 sm:max-w-sm z-50 rounded-2xl border border-[#222838] bg-[#14171E] shadow-2xl shadow-black/60 p-4 flex items-start gap-3"
    >
      <img
        src="/icons/icon-192.png"
        alt=""
        className="w-11 h-11 rounded-xl shrink-0"
      />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-gray-100">
          Install Mind at Ease
        </div>
        {deferredPrompt ? (
          <p className="text-xs text-gray-400 mt-0.5 leading-relaxed">
            Put the books on your home screen — one tap, full screen, opens
            even on a weak job-site signal.
          </p>
        ) : (
          <p className="text-xs text-gray-400 mt-0.5 leading-relaxed">
            Tap <Share size={12} className="inline -mt-0.5" /> Share, then
            &ldquo;Add to Home Screen&rdquo; — the books get their own icon,
            full screen, one tap away.
          </p>
        )}
        {deferredPrompt && (
          <button
            data-testid="install-prompt-accept"
            onClick={install}
            className="mt-2.5 inline-flex items-center gap-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 text-black text-xs font-semibold px-3 py-1.5"
          >
            <Download size={13} /> Install app
          </button>
        )}
      </div>
      <button
        data-testid="install-prompt-dismiss"
        onClick={dismiss}
        aria-label="Dismiss install prompt"
        className="text-gray-500 hover:text-gray-300 shrink-0"
      >
        <X size={16} />
      </button>
    </div>
  );
}
