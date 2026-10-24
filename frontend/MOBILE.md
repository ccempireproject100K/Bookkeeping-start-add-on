# Mobile apps (Capacitor wrap)

The bookkeeping web app ships as native Android / iOS apps via
[Capacitor](https://capacitorjs.com) — the **same codebase**, no rewrite.
The bundle inside the app is the production web build; it talks to the
production API exactly like the website does (`REACT_APP_BACKEND_URL` is
baked in at build time — see `scripts/check-build-env.js`). The PWA is
untouched and remains the no-store fallback.

| | |
|---|---|
| App id | `com.mindatease.bookkeeping` |
| App name | Mind at Ease (brand short name; full brand: Mind At Ease Solutions) |
| Web dir | `build/` (CRA output) |
| Config | `frontend/capacitor.config.json` |
| Native glue | `frontend/src/nativeShell.js` |
| Version | Android `versionCode 1` / `versionName "1.0.0"` in `android/app/build.gradle`; iOS in the Xcode project |

## Native behaviour added by the wrap

All of it lives in `src/nativeShell.js` and is a no-op in a browser:

- **Back button** (Android hardware/gesture): walks the SPA history;
  at the root it exits the app.
- **External links**: other origins and `mailto:` / `tel:` / `sms:` links
  open in the system browser / OS handler instead of navigating the
  webview away from the app (`@capacitor/browser`, `@capacitor/app`).
- **Safe areas**: `viewport-fit=cover` (`public/index.html`) plus
  `env(safe-area-inset-*)` padding on `#root` (`src/index.css`), so
  content clears notches and home indicators. Insets are 0 on the web.
- **Service worker**: skipped inside the native apps (their bundle ships
  with the app and updates arrive via store releases). The web/PWA
  registration is unchanged — see `src/index.js`.

Smoke coverage: `src/capacitorConfig.test.js` (config + scaffold on disk)
and `src/nativeShell.test.js` (link-routing rules).

## Icon & splash

Brand art comes from `public/icons/icon.svg` / `maskable.svg` (amber
gradient, dark "M"):

- **Android adaptive icon** is defined entirely in XML so it is
  reviewable in source: `res/drawable/ic_launcher_background.xml`
  (gradient), `res/drawable/ic_launcher_foreground.xml` (the M),
  `res/mipmap-anydpi-v26/*.xml`. The launch splash is
  `res/drawable/splash.xml` (brand dark #0B0D10 + centred mark), wired
  from `res/values/styles.xml`.
- **Raster PNGs** (legacy Android mipmaps, iOS `AppIcon` / `Splash`
  imagesets) are generated from the same SVGs. They are build artefacts:
  regenerate any time with
  [`@capacitor/assets`](https://github.com/ionic-team/capacitor-assets)
  using 1024×1024 / 2732×2732 exports of the SVGs, or Android Studio's
  Image Asset wizard. The generated set used for the first build is in
  the project folders in the build workspace.
- `gradle-wrapper.jar` is likewise a generated artefact of the Android
  template (produced by `cap add android` / any Gradle wrapper run).

## Build — Android

```bash
cd frontend
npm install            # yarn works too; npm needs --legacy-peer-deps
REACT_APP_BACKEND_URL=https://bookkeeping-api-ymh6.onrender.com npm run build
npx cap sync android
cd android
./gradlew assembleDebug      # debug APK (signed with the debug key)
./gradlew bundleRelease      # release AAB — needs the signing keystore
```

Release signing is intentionally **not** configured in the repo: the
owner's upload keystore and `keystore.properties` stay out of git (see
`STORE_SUBMISSION_CHECKLIST.md`).

## Build — iOS (macOS only)

iOS builds require a Mac with Xcode; the project in `ios/` is complete
and synced. On the Mac:

```bash
cd frontend
npm install
REACT_APP_BACKEND_URL=https://bookkeeping-api-ymh6.onrender.com npm run build
npx cap sync ios             # also runs pod install
npx cap open ios             # Xcode: select the App target
```

Then set the signing team (Apple Developer account), bump
`MARKETING_VERSION` / `CURRENT_PROJECT_VERSION` per release, and
Product → Archive → Distribute App → App Store Connect.

## Updating the apps

Web changes do not reach installed apps until a new native build is
released: rebuild the web bundle, `npx cap sync`, bump the version
(`versionCode` + `versionName` on Android; the two version fields in
Xcode on iOS), rebuild, and submit the new build to the stores.
