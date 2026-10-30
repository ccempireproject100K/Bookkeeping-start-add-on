#!/usr/bin/env python3
"""
i18n key parity test.
Fails (exit 1) if any key in en/translation.json is missing from any other language
folder under locales/ (every folder is checked, so a new language is covered as soon as
its folder exists), or if any language has keys not present in en (orphans).
Also validates that all files are well-formed JSON.

Usage: python3 tests/i18n-parity.py [--locales-dir PATH]
"""
import json
import sys
from pathlib import Path

def main():
    locales_dir = Path(sys.argv[sys.argv.index("--locales-dir") + 1]) if "--locales-dir" in sys.argv else Path(__file__).resolve().parents[1] / "frontend" / "src" / "locales"
    # Every locales/<code>/ folder is a language; en first (the source of truth).
    langs = ["en"] + sorted(p.name for p in locales_dir.iterdir() if p.is_dir() and p.name != "en")
    data = {}
    errors = []

    for lang in langs:
        p = locales_dir / lang / "translation.json"
        if not p.exists():
            errors.append(f"MISSING FILE: {p}")
            continue
        try:
            data[lang] = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"INVALID JSON in {lang}/translation.json: {e}")
            continue
        if not isinstance(data[lang], dict):
            errors.append(f"{lang}/translation.json is not a JSON object")
            continue

    if "en" not in data:
        print("FATAL: en/translation.json could not be loaded")
        for e in errors:
            print(" ", e)
        sys.exit(1)

    en_keys = set(data["en"].keys())

    # Empty-value check on en (likely extraction mistakes)
    for k in sorted(en_keys):
        v = data["en"][k]
        if not isinstance(v, str) or not v.strip():
            errors.append(f"en key '{k}' has empty/non-string value")

    for lang in ("es", "fr", "zh-CN", "hi", "ar"):
        if lang not in data:
            continue
        keys = set(data[lang].keys())
        for k in sorted(en_keys - keys):
            errors.append(f"MISSING key '{k}' in {lang}")
        for k in sorted(keys - en_keys):
            errors.append(f"ORPHAN key '{k}' in {lang} (not in en)")
        for k in sorted(en_keys & keys):
            v = data[lang][k]
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{lang} key '{k}' has empty/non-string value")

    # Interpolation placeholder check: {{...}} in en must appear in translations
    import re
    ph = re.compile(r"\{\{[^}]+\}\}")
    for k in sorted(en_keys):
        en_ph = set(ph.findall(str(data["en"][k])))
        if not en_ph:
            continue
        for lang in ("es", "fr", "zh-CN", "hi", "ar"):
            if lang not in data or k not in data[lang]:
                continue
            tr_ph = set(ph.findall(str(data[lang][k])))
            if en_ph != tr_ph:
                errors.append(f"PLACEHOLDER MISMATCH in {lang} key '{k}': en={sorted(en_ph)} vs {lang}={sorted(tr_ph)}")

    print(f"en keys: {len(en_keys)}")
    for lang in ("es", "fr", "zh-CN", "hi", "ar"):
        if lang in data:
            print(f"{lang} keys: {len(data[lang])}")

    if errors:
        print(f"\nFAILED: {len(errors)} problem(s)")
        for e in errors[:50]:
            print(" -", e)
        if len(errors) > 50:
            print(f" ... and {len(errors) - 50} more")
        sys.exit(1)

    print("\nPASS: all languages have full key parity with en")
    sys.exit(0)

if __name__ == "__main__":
    main()
