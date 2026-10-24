# Store submission checklist — Mind at Ease (bookkeeping app)

One page. **DONE** = already built in the mobile-apps PR. **OWNER** =
needs the owner's accounts / money / signature. Do not improvise the
OWNER items from any other account.

## Already DONE (in the PR)

- [x] Capacitor wrap of the existing web app — same codebase, no rewrite
- [x] Android + iOS projects scaffolded (`frontend/android`, `frontend/ios`)
- [x] App id `com.mindatease.bookkeeping`, name **Mind at Ease**
- [x] Brand icon + splash (from `public/icons/*.svg`) on both platforms
- [x] Android debug APK built and verified (installable; in the build
      workspace — `MindAtEase-debug.apk`)
- [x] App configured against the production API
- [x] Native back-button, external-link and safe-area behaviour
- [x] PWA kept intact as the no-store fallback

## OWNER — Google Play (one-time $25)

1. Create a **Google Play Console** developer account — **$25 one-time**;
   complete Google's identity verification.
2. **Create the upload keystore** (do this once, guard the file — with
   Play App Signing Google can reset a lost upload key, but the process
   takes days):
   `keytool -genkeypair -v -keystore upload-keystore.jks -keyalg RSA -keysize 2048 -validity 10000 -alias upload`
   Put the keystore + a `keystore.properties` (storeFile, storePassword,
   keyAlias, keyPassword) **outside git**; wire a `signingConfig` for
   release in `android/app/build.gradle` pointing at it.
3. Build the release bundle: `./gradlew bundleRelease` → `app-release.aab`.
4. In Play Console: create the app (name "Mind at Ease"), upload the AAB,
   and complete the store forms:
   - **Data safety** section (the app handles accounts + financial data —
     declare what the API collects; match the privacy policy)
   - **Privacy policy URL** (must be live on the web)
   - Content rating questionnaire, target audience, app category (Finance)
   - Store listing: icon (512×512 provided in the build workspace),
     screenshots, descriptions
5. **New-account rule:** personal developer accounts created after
   Nov 2023 must run a **closed test with at least 12 testers for 14
   days** before production release. Budget the two weeks.
6. Submit for review → production rollout.

## OWNER — Apple App Store ($99/year)

1. Join the **Apple Developer Program** — **$99/year** (organization
   enrolment needs a D-U-N-S number; individual enrolment is faster).
2. On a **Mac with Xcode**: open `frontend/ios/App/App.xcworkspace`
   (run `npx cap sync ios` first so CocoaPods are installed).
3. Select the App target → Signing & Capabilities → choose your Team
   (Xcode manages the certificate/profile).
4. Set the version/build numbers, then **Product → Archive** →
   Distribute App → App Store Connect upload.
5. In **App Store Connect**: create the app record (bundle id
   `com.mindatease.bookkeeping`), complete:
   - **Privacy "nutrition labels"** (match the privacy policy)
   - Age rating questionnaire, category (Finance), pricing (Free)
   - Screenshots (from a device/simulator), description, keywords
   - **A demo account** for the reviewer (the app requires sign-in —
     Apple rejects finance apps they cannot log into)
6. Submit for review. Finance/bookkeeping apps get extra scrutiny —
   answer the review team's questions promptly.

## Every release after the first

1. `REACT_APP_BACKEND_URL=https://bookkeeping-api-ymh6.onrender.com npm run build`
2. `npx cap sync`
3. Bump the version: Android `versionCode` +1 / new `versionName`
   (`android/app/build.gradle`); iOS build number + version in Xcode.
4. Rebuild (AAB / Archive) and upload to the same store listings.
