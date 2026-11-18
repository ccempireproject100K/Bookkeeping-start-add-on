# Mind at Ease Books — Google Play package (TWA)

A Trusted Web Activity wrapper around the Bookkeeping PWA at
`mindateasesolutions.com`. The app on a phone is the same web app the PWA
ships — this package only gives it a Play Store door, a home-screen icon,
and full-screen (no browser chrome) presentation once Digital Asset Links
verify.

- Package id: `com.mindatease.bookkeeping`
- Host: `https://mindateasesolutions.com/`
- Engine: `com.google.androidbrowserhelper:androidbrowserhelper` (the same
  library Bubblewrap generates projects around) — hand-written minimal
  project, no Bubblewrap CLI needed.
- Digital Asset Links: `frontend/public/.well-known/assetlinks.json`
  (goes live with the PWA branch deploy) names this package id + the
  signing certificate fingerprint from `../keys/`.

## Build the release bundle (.aab)

Prerequisites: JDK 17, Android SDK platform 35 + build-tools 35, Gradle 8.9+.

```bash
cd twa
echo "sdk.dir=/path/to/android-sdk" > local.properties
export TWA_STORE_PASSWORD="$(cat ../keys/KEYSTORE-PASSWORD.txt)"
export TWA_KEY_PASSWORD="$TWA_STORE_PASSWORD"
gradle :app:bundleRelease
# → app/build/outputs/bundle/release/app-release.aab
```

The signing config reads the keystore from `../keys/` and the passwords
from the environment — no secret ever lives in this project. See
`../keys/README.md`: **the keystore is this app's identity; losing it
means the app can never be updated.**

## Build status at prep time (2026-11-14)

The project is complete and committed, but the AAB itself is **not yet
built**: the sandbox's Gradle daemon is killed by the known child-process
write fault (AGENTS.md, 2026-11-11) and the retry was permission-declined.
Build it on any healthy machine with the command above — nothing else is
missing.
