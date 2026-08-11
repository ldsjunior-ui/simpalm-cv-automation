#!/usr/bin/env python3
"""
debug_cv.py — Local parser smoke-test. Run BEFORE any push.
Usage:
    python3 scripts/debug_cv.py /path/to/cv.pdf
    python3 scripts/debug_cv.py                    # tests all files in inbox/
"""
import sys
import re
import json
from pathlib import Path

# Make sure process_cv is importable from repo root
sys.path.insert(0, str(Path(__file__).parent))
from process_cv import extract_text, fix_char_spacing, parse_cv, llm_extract_cv, COMPANY_INDICATOR_WORDS, ROLE_TITLE_WORDS

INBOX = Path(__file__).parent.parent / "inbox"

def test_cv(path: str) -> dict:
    ext = Path(path).suffix.lower()
    # extract_text() already runs fix_char_spacing() internally for both the PDF and DOCX
    # branches (see extract_text_pdf()/extract_text_docx()) — calling it again here, as the
    # previous version of this script did, was a harmless-in-practice but redundant double
    # application. Mirror main()'s actual code exactly: extract once, don't re-clean.
    text = extract_text(path)
    # .txt/.text files take a DIFFERENT code path in production (main(), ~line 2705): they skip
    # the regex pipeline entirely and go straight to llm_extract_cv(), with parse_cv() only as a
    # fallback if the LLM call fails. Before this fix, debug_cv.py always called parse_cv()
    # regardless of extension — meaning it silently tested a code path .txt files NEVER actually
    # take in production, and its default no-args sweep didn't even glob .txt files at all. Since
    # the pipeline's docs describe .txt as the PRIMARY input format ("Pipeline agora recebe APENAS
    # .txt"), the local pre-push verification tool was blind to its own main input format.
    if ext in ('.txt', '.text'):
        text = fix_char_spacing(text)  # mirrors the same fix now in main() — see its comment
        data = llm_extract_cv(text, max_tokens=2048)
        if not data:
            print("   ⚠️  LLM unavailable (no ANTHROPIC_API_KEY, or the call failed) — falling back "
                  "to the regex parser. This does NOT prove the real .txt/LLM path is correct; it only "
                  "proves parse_cv()'s fallback is. Set ANTHROPIC_API_KEY to test the actual production path.")
            data = parse_cv(text)
    else:
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

    # These four did NOT exist before 2026-08-11 and are the reason a batch of real inbox CVs
    # kept reporting "✅ passed" while their candidate_title/candidate_location were visibly a
    # company name, a language-proficiency statement, or a citizenship claim — none of the
    # checks above catch that CLASS of error, only bullets/length/trailing-period shape.
    def _word_hits(s, wordset):
        return sum(1 for w in re.findall(r"[a-zA-Z']+", s.lower()) if w in wordset)

    if title:
        _company_hits, _role_hits = _word_hits(title, COMPANY_INDICATOR_WORDS), _word_hits(title, ROLE_TITLE_WORDS)
        if _company_hits > 0 and _role_hits == 0:
            flags.append("❌ TITLE_LOOKS_LIKE_COMPANY")
        if title.count('(') >= 2:
            flags.append("❌ TITLE_LOOKS_LIKE_LANGUAGE_STMT")
    if loc:
        _lc_hits, _lr_hits = _word_hits(loc, COMPANY_INDICATOR_WORDS), _word_hits(loc, ROLE_TITLE_WORDS)
        if _lc_hits > 0 and _lr_hits == 0:
            flags.append("❌ LOCATION_LOOKS_LIKE_COMPANY")
        if any(w in loc.lower() for w in ('citizenship', 'nationality', 'work authorization', 'visa status')):
            flags.append("❌ LOCATION_LOOKS_LIKE_CITIZENSHIP_STMT")

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
        # .txt/.text were missing here entirely — the default "test everything in inbox" sweep
        # never exercised the pipeline's primary input format at all (see the comment in test_cv()).
        paths = (sorted(INBOX.glob("*.pdf")) + sorted(INBOX.glob("*.docx"))
                 + sorted(INBOX.glob("*.txt")) + sorted(INBOX.glob("*.text")))
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
