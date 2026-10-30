# Adding a language

The app's languages live in one registry: `SUPPORTED_LANGUAGES` in
`frontend/src/lib/i18n.js`. The switcher, browser-language detection
(`pt-BR` → `pt`), Intl number/money formatting and page direction all read it.

## Steps

1. **Translations.** Copy `frontend/src/locales/en/translation.json` to
   `frontend/src/locales/<code>/translation.json` and translate every value.
   Keep keys, `{{placeholders}}` and `<tags>` exactly; brand names
   (Mind at Ease, Momo, Empire) stay as they are.
2. **Registry.** In `frontend/src/lib/i18n.js`:
   - add `import <name> from "@/locales/<code>/translation.json";`
   - add `<code>: <name>,` to `RESOURCES`
   - add `{ code, label, flag, intl }` to `SUPPORTED_LANGUAGES`, where `label`
     is the language's own name (e.g. "Tiếng Việt") and `intl` is the Intl
     locale (use `-u-nu-latn` to keep 0-9 digits if the language defaults to
     other numerals).
3. **Plurals.** This app has no `_one`/`_other` plural keys today, so there is
   nothing extra to add. (If plural keys are introduced later, languages such as
   Arabic or Russian need extra `_few`/`_many`… forms, and `tests/i18n-parity.py`
   must be taught to allow them.)
4. **Right to left.** Nothing to do: i18next knows RTL languages
   (ar, he, fa, ur, …) and the page sets `<html dir="rtl">` automatically.
   Check the main pages at phone width afterwards.
5. **Check.** `python3 tests/i18n-parity.py` and `cd frontend && yarn test`.
   Both pick up the new folder automatically and fail on missing keys or a
   registry/folder mismatch.
6. **Review.** Machine-assisted translations need a fluent speaker's sign-off
   before the language is offered to customers.
