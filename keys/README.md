# Books Android release keystore — READ THIS FIRST

`mindatease-books-release.keystore` (alias `mindatease-books`) is the
signing identity of the **Mind at Ease** Android app
(`com.mindatease.bookkeeping`) on Google Play.

**This keystore is the app's identity. If it is lost, the app can never
be updated again** — Google Play requires every update to be signed with
the original key. A lost key means abandoning the listing and starting a
new app from zero (new package, no reviews, no installs carried over).

Rules:

- The keystore file and `KEYSTORE-PASSWORD.txt` live together. Back both
  up, together, to at least one place outside this machine (owner's
  password manager / vault). Neither file is committed to git — this
  README is the only file from this folder that is.
- Never email, upload, or share the keystore or its password outside
  Mind At Ease Solutions.
- The AAB in `twa/` is signed with this key. Its SHA-256 certificate
  fingerprint is published in
  `frontend/public/.well-known/assetlinks.json` (Digital Asset Links) —
  if the fingerprint there ever stops matching this key, the Android app
  loses its verified link handling and falls back to browser behavior.

Fingerprint (SHA-256) at creation, 2026-11-14:
`CD:94:FC:37:C4:D9:2F:CC:3B:B9:B3:EE:E7:EC:DD:27:EB:AA:D3:4D:25:B2:FA:5F:CF:9D:F1:7A:F6:4E:73:41`
