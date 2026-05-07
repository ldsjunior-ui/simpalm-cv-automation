#!/usr/bin/env python3
"""
debug_cv.py — Local parser smoke-test. Run BEFORE any push.
Usage:
    python3 scripts/debug_cv.py /path/to/cv.pdf
    python3 scripts/debug_cv.py                    # tests all files in inbox/
"""
import sys
import json
from pathlib import Path

# Make sure process_cv is importable from repo root
sys.path.insert(0, str(Path(__file__).parent))
from process_cv import extract_text, fix_char_spacing, parse_cv

INBOX = Path(__file__).parent.parent / "inbox"

def test_cv(path: str) -> dict:
    text = extract_text(path)
    text = fix_char_spacing(text)
    data = parse_cv(text)
    return data

def report(path: str, data: dict):
    name  = data["candidate_name"]
    title = data["candidate_title"]
    loc   = data["candidate_location"]
    yoe   = data.get("years_experience", "?")
    exp   = len(data.get("experience", []))
    skills = len(data.get("skills", []))

    # Quality flags
    flags = []
    if name in ("Candidate", "Unknown Candidate", ""):
        flags.append("❌ NAME_FAIL")
    if " " not in name and len(name) > 8 and name == name.upper():
        flags.append("❌ NAME_NO_SPACE")
    if title and title[0] in ("•", "▪", "▸"):
        flags.append("❌ TITLE_BULLET")
    if title and len(title.split()) > 12:
        flags.append("❌ TITLE_TOO_LONG")
    if loc and len(loc) > 80:
        flags.append("❌ LOCATION_SENTENCE")
    if loc and loc.rstrip().endswith(".") and len(loc.split()) > 3:
        flags.append("❌ LOCATION_PERIOD_SENTENCE")
    if exp == 0:
        flags.append("⚠️  NO_EXPERIENCE")

    status = "✅" if not flags else " ".join(flags)

    print(f"\n{'─'*60}")
    print(f"File:     {Path(path).name}")
    print(f"Status:   {status}")
    print(f"Name:     {name!r}")
    print(f"Title:    {title!r}")
    print(f"Location: {loc!r}")
    print(f"YoE:      {yoe}  |  Roles: {exp}  |  Skills: {skills}")
    if flags:
        print(f"FLAGS:    {' | '.join(flags)}")

def main():
    if len(sys.argv) > 1:
        paths = sys.argv[1:]
    else:
        paths = sorted(INBOX.glob("*.pdf")) + sorted(INBOX.glob("*.docx"))
        if not paths:
            print("No CV files found in inbox/")
            return

    print(f"Testing {len(paths)} CV(s)...\n")
    fail = 0
    for p in paths:
        try:
            data = test_cv(str(p))
            report(str(p), data)
            flags = []
            if data["candidate_name"] in ("Candidate", "Unknown Candidate", ""):
                flags.append("NAME_FAIL")
            if flags:
                fail += 1
        except Exception as e:
            print(f"\n❌ CRASH on {p}: {e}")
            import traceback; traceback.print_exc()
            fail += 1

    print(f"\n{'═'*60}")
    print(f"Result: {len(paths) - fail}/{len(paths)} passed")
    if fail:
        sys.exit(1)

if __name__ == "__main__":
    main()
