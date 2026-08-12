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
from process_cv import (extract_text, fix_char_spacing, parse_cv, llm_extract_cv_cascade,
                         COMPANY_INDICATOR_WORDS, ROLE_TITLE_WORDS, reconstruct_broken_lines,
                         looks_like_collapsed_txt, looks_like_plausible_name, COLLAPSED_TXT_WARNING)

INBOX = Path(__file__).parent.parent / "inbox"

def test_cv(path: str) -> dict:
    ext = Path(path).suffix.lower()
    # extract_text() already runs fix_char_spacing() internally for both the PDF and DOCX
    # branches (see extract_text_pdf()/extract_text_docx()) — calling it again here, as the
    # previous version of this script did, was a harmless-in-practice but redundant double
    # application. Mirror main()'s actual code exactly: extract once, don't re-clean.
    text = extract_text(path)
    # .txt/.text files now take the SAME priority as PDF/DOCX in production (main(), 2026-08-11
    # flip): parse_cv() runs FIRST (reconstructing line breaks first if the text looks collapsed),
    # LLM cascade (Ollama local -> Anthropic -> OpenRouter) only fires if the local result's name
    # isn't plausible. This replaces the 2026-05-07 "always LLM for plain .txt" design, an
    # untested speed assumption falsified by this session's own Ollama-latency-under-load data
    # (see process_cv.py's main() comment for the full rationale). Mirror main()'s real code path
    # exactly here — debug_cv.py testing a path production doesn't actually take defeats the point
    # of a pre-push local verification tool (this exact blind spot bit this pipeline before).
    if ext in ('.txt', '.text'):
        text = fix_char_spacing(text)  # mirrors the same fix now in main() — see its comment
        is_collapsed = looks_like_collapsed_txt(text)
        text_to_parse = text
        if is_collapsed:
            print("   🔧 Looks like a collapsed/pasted dump — reconstructing line breaks first.")
            text_to_parse = reconstruct_broken_lines(text)
        else:
            print("   ⚡ Plain-text mode: trying the local regex parser first.")
        local_data = parse_cv(text_to_parse)
        if looks_like_plausible_name(local_data.get("candidate_name", "")):
            print("   ✅ Local regex parse recovered a plausible name — LLM call skipped entirely.")
            data = local_data
        else:
            print("   ⚠️  Local parse wasn't enough on its own — trying the LLM cascade "
                  "(Ollama local first, then paid APIs only if needed).")
            data = llm_extract_cv_cascade(text_to_parse, max_tokens=2048)
            if not data:
                print("   ⚠️  No LLM rung available (Ollama not running and no ANTHROPIC_API_KEY/"
                      "OPENROUTER_API_KEY) — using the local parse result as-is.")
                data = local_data
                if not looks_like_plausible_name(data.get("candidate_name", "")):
                    _guess = data.get("candidate_name", "")
                    data["candidate_name"] = f"{COLLAPSED_TXT_WARNING} — {Path(path).name} (regex guess: {_guess!r})"
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
    if name in ("Candidate", "Unknown Candidate", "") or name.startswith(COLLAPSED_TXT_WARNING):
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
            _cand_name = data["candidate_name"]
            if _cand_name in ("Candidate", "Unknown Candidate", "") or _cand_name.startswith(COLLAPSED_TXT_WARNING):
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
