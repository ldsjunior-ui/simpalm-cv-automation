#!/usr/bin/env python3
"""
Simpalm Staffing — CV Automation Pipeline
Parses PDF/DOCX CVs → structured JSON → Simpalm-branded PDF via WeasyPrint
100% open source, zero API keys required.
"""

import os
import re
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime

# ── Extraction ───────────────────────────────────────────────────────────────

def fix_char_spacing(text: str) -> str:
    """Fix PDFs where text is extracted as 'J U A N   C A R L O S' → 'JUAN CARLOS'."""
    # Replace Private Use Area characters from Symbol/Wingdings fonts.
    # Common examples: \uf0b7 → bullet, \uf0a7 → bullet, \uf0fc → checkbox.
    # Normalise them all to a standard bullet so downstream parsing works correctly.
    text = re.sub(r'[\uf000-\uf0ff]', '•', text)

    lines = text.splitlines()
    fixed = []
    for line in lines:
        stripped = line.strip()
        tokens = stripped.split(' ')
        # Detect spaced-char pattern: >= 60% of tokens are single characters
        single_chars = sum(1 for t in tokens if len(t) == 1 and t.isalpha())
        if len(tokens) >= 6 and single_chars / len(tokens) >= 0.6:
            # Collapse: group single chars into words separated by 2+ spaces
            collapsed = re.sub(r'(?<=\w) (?=\w)', '', stripped)
            # Restore word breaks (2+ spaces become single space)
            collapsed = re.sub(r'  +', ' ', collapsed)
            fixed.append(collapsed)
        else:
            fixed.append(line)
    return '\n'.join(fixed)

def extract_text_pdf(path: str) -> str:
    from pdfminer.high_level import extract_text
    raw = extract_text(path)
    return fix_char_spacing(raw)

def extract_text_docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    raw = "\n".join(p.text for p in doc.paragraphs)
    return fix_char_spacing(raw)

def extract_text(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return extract_text_pdf(path)
    elif ext in (".docx", ".doc"):
        return extract_text_docx(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

# ── Section Detection ─────────────────────────────────────────────────────────

SECTION_PATTERNS = {
    "summary":        r"(?i)^(professional\s+)?summary|profile|about\s+me|objective|overview",
    "experience":     r"(?i)^(professional\s+|work\s+)?experience|employment|career\s+history|work\s+history",
    "education":      r"(?i)^education|academic|qualification|degree|university|college",
    "skills":         r"(?i)^(core\s+|key\s+|technical\s+|hard\s+|soft\s+)?skills?|competenc|expertise|technologies|tools",
    "languages":      r"(?i)^languages?",
    "certifications": r"(?i)^(?:certific\w*|licens\w*|accredit\w*|credential\w*)",
    "projects":       r"(?i)^projects?|portfolio|key\s+projects?",
    "awards":         r"(?i)^awards?|honors?|achievements?|recognition|publications?",
    "volunteer":      r"(?i)^volunteer|community|non.?profit",
}

def split_sections(text: str) -> dict:
    lines = text.splitlines()
    sections = {
        "_header": [], "summary": [], "experience": [], "education": [],
        "skills": [], "languages": [], "certifications": [], "projects": [],
        "awards": [], "volunteer": [], "_overflow": [],
    }
    current = "_header"

    for line in lines:
        stripped = line.strip()
        matched = False
        for section, pattern in SECTION_PATTERNS.items():
            # Guard 1: true section headings are short (≤ 60 chars).
            # Prevents "Experience in setting up Azure Data Factory..." from matching.
            m_sec = re.match(pattern, stripped)
            if not m_sec or len(stripped) > 60:
                continue
            # Guard 2: reject label lines like "Project: Enterprise Commerce Volume License"
            # where the keyword is immediately followed by ": content".
            # True headings are never "Keyword: some long value".
            remaining_after_kw = stripped[m_sec.end():].lstrip()
            if remaining_after_kw.startswith(":"):
                continue
            # Guard 3: reject sentence continuations like "Experience with ticketing…"
            # or "Experience in setting up…". A true heading never starts with these
            # relational prepositions immediately after the section keyword.
            _first_word = remaining_after_kw.split()[0].lower() if remaining_after_kw.split() else ""
            if _first_word in {"in", "with", "on", "as", "for", "at", "to", "from"}:
                continue
            current = section
            matched = True
            # If the header line also contains content (e.g. "SKILLS • foo • bar"),
            # keep the part after the first word/symbol as section content
            after = re.sub(pattern, "", stripped, count=1, flags=re.IGNORECASE).lstrip(" :–-•·▸►▪")
            if after:
                sections[current].append(after)
            break
        if not matched:
            # Detect unrecognised headings: short, mostly uppercase, no lowercase letters
            if (stripped and len(stripped) <= 40
                    and stripped == stripped.upper()
                    and re.search(r'[A-Z]', stripped)
                    and current not in ("_header",)):
                # Stash unrecognised heading content into _overflow
                current = "_overflow"
                sections[current].append(line)
            else:
                sections[current].append(line)

    return {k: "\n".join(v).strip() for k, v in sections.items()}

# ── Contact / Header Parsing ──────────────────────────────────────────────────

EMAIL_RE    = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}")
PHONE_RE    = re.compile(r"[\+]?[\d][\d\s\-().]{7,16}[\d]")
LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w\-]+", re.IGNORECASE)

LOCATION_KEYWORDS = [
    "brazil", "brasil", "remote", "international", "usa", "uk", "canada",
    "mexico", "colombia", "argentina", "spain", "españa", "madrid", "barcelona",
    "portugal", "chile", "peru", "brasília", "são paulo", "rio de janeiro",
    "distrito federal", "new york", "miami", "california", "london",
]

def parse_header(header_text: str) -> dict:
    lines = [l.strip() for l in header_text.splitlines() if l.strip()]

    name     = lines[0] if lines else "Candidate"
    email    = next((EMAIL_RE.search(l).group() for l in lines if EMAIL_RE.search(l)), "")
    phone    = next((PHONE_RE.search(l).group().strip() for l in lines if PHONE_RE.search(l)), "")
    linkedin = next((LINKEDIN_RE.search(l).group() for l in lines if LINKEDIN_RE.search(l)), "")

    # Classify remaining lines as location or title
    location = ""
    title    = ""
    for line in lines[1:]:
        if EMAIL_RE.search(line) or PHONE_RE.search(line) or LINKEDIN_RE.search(line):
            continue
        ll = line.lower()
        is_location = any(kw in ll for kw in LOCATION_KEYWORDS)
        if is_location and not location:
            location = line
        elif not title and not is_location and len(line) < 120:
            title = line

    initials = "".join(w[0].upper() for w in name.split()[:2])

    return {
        "candidate_name":     name,
        "candidate_initials": initials,
        "candidate_title":    title,
        "candidate_email":    email,
        "candidate_phone":    phone,
        "candidate_location": location,
        "candidate_linkedin": linkedin,
    }

# ── Skills Parsing ────────────────────────────────────────────────────────────

_SKILLS_NARRATIVE_START_RE = re.compile(
    r'^(?:experience\s+in|hands[\s\-]+on|good\s+knowledge|working\s+knowledge|'
    r'strong\s+|excellent\s+|proficient\s+|skilled\s+in|ability\s+to|'
    r'expertise\s+in|responsible\s+for|knowledge\s+of|familiar\s+with|'
    r'understanding\s+of|in\s+the\s+areas?\s+of|exposure\s+to|'
    r'involved\s+in|worked\s+on|worked\s+with)',
    re.IGNORECASE
)


def parse_skills(skills_text: str) -> list:
    if not skills_text:
        return []
    # Split by common delimiters: comma, pipe, bullet, newline, semicolon, parentheses
    raw = re.split(r"[,|•·;\n\(\)]+", skills_text)
    # Generic category labels that aren't specific skills (filter out)
    _GENERIC = {
        "methodologies","tools","technologies","skills","areas","responsibilities",
        "reporting","ticketing","environment","technology","scripting","languages",
        "platforms","frameworks","databases","os","operating systems","cloud",
        "services","applications","software","systems","others","additional",
        "technical","professional","core","key","primary","secondary",
    }
    skills = []
    seen = set()
    for s in raw:
        s = s.strip().strip("–-•·▸►▪").rstrip('.')
        sl = s.lower()
        # Reject: empty, too short/long, generic labels, punctuation-only
        if not s or not (2 < len(s) < 45):
            continue
        if sl in seen or sl in _GENERIC:
            continue
        if re.match(r'^[&/\-]+$', s):
            continue
        # Reject narrative sentence openers ("Experience in the areas of X")
        if _SKILLS_NARRATIVE_START_RE.match(s):
            continue
        # Reject obvious sentence continuations: start lowercase
        # (comma-split from mid-sentence; exceptions: tech abbreviations like "iOS", "pH")
        if s[0].islower() and not re.match(r'^(iOS|macOS|pH|eBay|eCommerce)\b', s):
            continue
        # Reject hyphenation artifacts: "Re" or short cap word at end after "and" (line-break splits)
        if re.search(r'\s+and\s+[A-Z][a-z]{0,2}$', s):
            continue
        # Reject truncated items ending with "&" or "–" or "/"
        if s[-1] in ('&', '–', '/', '\\'):
            continue
        skills.append(s)
        seen.add(sl)
    return skills[:20]  # cap at 20 — sidebar pill grid looks best at ≤ 20

# ── Language Parsing ──────────────────────────────────────────────────────────

LEVEL_MAP = {
    "native":      100, "bilingual":   100,
    "fluent":       90, "advanced":     80,
    "upper":        75, "intermediate": 60,
    "basic":        35, "beginner":     25,
    "elementary":   30, "professional": 85,
}

# Known human (natural) languages for validation.
# Some CVs label a "Languages" section to list programming/tech languages instead.
# Any parsed result whose name doesn't appear here is treated as a tech skill, not
# a human language — preventing "Azure", "Python", "Docker" from polluting the
# sidebar language bars.
_HUMAN_LANGS = {
    "english","spanish","portuguese","french","german","italian","mandarin",
    "chinese","arabic","russian","japanese","korean","hindi","dutch","turkish",
    "polish","swedish","norwegian","danish","finnish","greek","hebrew","thai",
    "vietnamese","indonesian","malay","tagalog","urdu","bengali","punjabi",
    "catalan","czech","slovak","romanian","hungarian","ukrainian","bulgarian",
    "serbian","croatian","slovenian","latvian","lithuanian","estonian",
    "swahili","yoruba","igbo","amharic","farsi","persian","pashto","nepali",
    "sinhala","tamil","telugu","kannada","malayalam","gujarati","marathi",
    "azerbaijani","kazakh","uzbek","georgian","armenian","albanian","macedonian",
    "bosnian","maltese","icelandic","irish","welsh","basque","galician",
}

def _is_human_language(name: str) -> bool:
    """Return True only if the name looks like a human/natural language."""
    n = name.lower().strip()
    # Hard disqualifiers: contains digits, dots (e.g. "Node.js"), slashes, hyphens
    # that suggest tech (e.g. "CI/CD"), or is ALL_CAPS acronym (e.g. "SQL", "AWS")
    if re.search(r'\d', n):
        return False
    if re.search(r'[/\\.@#]', n):
        return False
    # Pure-uppercase 2–5 char acronym → tech term
    if re.fullmatch(r'[A-Z]{2,5}', name.strip()):
        return False
    # Long multi-word entries are likely tool descriptions, not language names
    if len(n.split()) > 3:
        return False
    # Check against known human language list (prefix match to handle "Mandarin Chinese" etc.)
    for lang in _HUMAN_LANGS:
        if n.startswith(lang) or lang.startswith(n):
            return True
    return False

def parse_languages(lang_text: str) -> list:
    if not lang_text:
        return []
    raw_lines = lang_text.splitlines()
    entries = []
    for line in raw_lines:
        parts = re.split(r"[•·|/\\]", line)
        entries.extend(parts)

    languages = []
    for entry in entries:
        entry = entry.strip().strip("–-•·▸►▪()")
        if not entry or len(entry) < 2:
            continue
        level = "Conversational"
        percent = 50
        for kw, pct in LEVEL_MAP.items():
            if kw in entry.lower():
                level = kw.capitalize()
                percent = pct
                break
        lang_name = re.split(r"[-–:|,\(]", entry)[0].strip()
        lang_name = re.sub(r"\b(" + "|".join(LEVEL_MAP.keys()) + r")\b", "", lang_name, flags=re.IGNORECASE).strip()
        lang_name = re.sub(r"\s+", " ", lang_name).strip()
        if lang_name and 1 < len(lang_name) < 40 and _is_human_language(lang_name):
            languages.append({"name": lang_name, "level": level, "percent": percent})
    return languages

# ── Education Parsing ─────────────────────────────────────────────────────────

DEGREE_RE = re.compile(
    r"(?i)(b\.?sc?\.?|m\.?sc?\.?|ph\.?d\.?|mba|bachelor|master|post.?grad|graduate|associate|diploma|licenc)",
)

# Matches university/institution keyword so we can split compact single-line entries:
# "Bachelor of Laws (LL.B.) Universidade do Vale do Taquari – Univates, 2018"
_UNIV_SPLIT_RE = re.compile(
    r"\s+(universidade|university|college|instituto|institute|school|academia|polytechnic|faculty)\b",
    re.IGNORECASE,
)

def parse_education(edu_text: str) -> list:
    if not edu_text:
        return []
    education = []
    blocks = re.split(r"\n{2,}", edu_text)
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        degree = ""
        institution = ""

        # Compact single-line format: "Bachelor of Laws University of X, 2018"
        # Detect: only one line AND it contains a degree keyword
        if len(lines) == 1 and DEGREE_RE.search(lines[0]):
            # Strip leading bullet/symbol chars before processing
            line = lines[0].lstrip("•·▸►▪-– \t")
            mu = _UNIV_SPLIT_RE.search(line)
            if mu:
                degree      = line[:mu.start()].strip().rstrip(".")
                # Remove trailing year patterns like "2018", "in 2014", ", 2014"
                raw_inst    = line[mu.start():].strip()
                institution = re.sub(r"[\s,]*\bin\s+\d{4}\b|\s*,?\s*\d{4}\s*$", "", raw_inst).strip()
            else:
                # No university keyword — strip trailing year and use as degree only
                degree = re.sub(r"[\s,]*\bin\s+\d{4}\b|\s*,?\s*\d{4}\s*$", "", line).strip().rstrip(".")
            if degree:
                education.append({"degree": degree, "institution": institution})
            continue

        for line in lines:
            # Strip leading bullet/symbol characters before matching
            clean = line.lstrip("•·▸►▪-– \t")
            if DEGREE_RE.search(clean) and not degree:
                degree = clean
            elif degree and not institution:
                institution = clean
        if degree:
            # Strip trailing punctuation artifacts from degree line
            degree = degree.rstrip(".")
            education.append({"degree": degree, "institution": institution})
    return education  # no cap

# ── Experience Parsing ────────────────────────────────────────────────────────

DATE_RANGE_RE = re.compile(
    # Matches date ranges in many CV formats:
    #   "November 2024 – Present"   "Oct 2023 – Apr 2024"   "2024 – Present"
    #   "2018–2019"                 "11/2023 – 12/2024"     "2012 to 2016"
    #   "Mar 2022 Till Date"        (Indian English consulting format)
    r"(?:(?:\d{1,2}/)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?\s+))?"
    r"\d{4}"
    r"\s*(?:[-–—]+|to|till)\s*"
    r"(?:(?:\d{1,2}/)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?\s+))?"
    r"(?:present|current|now|date|\d{4})",
    re.IGNORECASE
)

# ── Table-format experience helpers ──────────────────────────────────────────

_EXP_TABLE_HEADER_KWS = frozenset({
    'duration', 'organization', 'organisation', 'designation',
    'role responsibilities', 'segment', 'domain', 'employer',
    'company', 'designation/', 'role/', 'role'
})

def _looks_like_experience_table(lines: list) -> bool:
    """Return True when the first few non-empty lines look like table column headers."""
    non_empty = [l.strip().lower() for l in lines if l.strip()][:10]
    chunk = ' '.join(non_empty)
    hits = sum(1 for kw in ('duration', 'organization', 'organisation', 'designation', 'employer')
               if kw in chunk)
    return hits >= 2


def _parse_table_experience(lines: list) -> list:
    """
    Reconstruct experience entries from a multi-column table extracted column-first
    by pdfminer.  Pattern after pdfminer reads a 3–4 column table:

      [header row keywords — skip]
      [all date values grouped]       ← left column extracted first
      [org1] [role1] [resp1]          ← remaining columns, row by row
      [org2] [role2] [resp2]
      ...

    Strategy:
    1. Skip lines that are pure table header keywords.
    2. Split into groups separated by blank lines.
    3. Classify groups as date-groups or content-groups.
    4. Pair each date with the next (org, role, resp) triplet.
    """
    BULLET_CHARS = ("•", "·", "▸", "►", "▪", "-", "–")

    # ── Step 1: Split into blank-line groups ──────────────────────────────────
    groups: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            if cur:
                groups.append(cur)
                cur = []
        else:
            cur.append(s)
    if cur:
        groups.append(cur)

    # ── Step 2: Classify each group ───────────────────────────────────────────
    date_values: list[str] = []
    content_groups: list[str] = []   # merged text of non-date, non-header groups

    for group in groups:
        joined = ' '.join(group).strip()
        joined_lower = joined.lower().rstrip('/')

        # Skip pure header keyword groups
        if joined_lower in _EXP_TABLE_HEADER_KWS:
            continue
        # Skip multi-word header phrases
        if all(w.lower().rstrip('/') in _EXP_TABLE_HEADER_KWS for w in re.split(r'[\s/]+', joined)):
            continue

        # Classify as date if DATE_RANGE_RE matches (and group has no bullet)
        m = DATE_RANGE_RE.search(joined)
        if m and not any(joined.startswith(c) for c in BULLET_CHARS):
            # Accept as date only if the non-date remainder is short/empty
            remainder = DATE_RANGE_RE.sub('', joined).strip().rstrip('–-|·, ')
            if len(remainder) < 15:
                date_values.append(m.group().strip())
                continue

        content_groups.append(joined)

    if not date_values:
        return []   # Can't reconstruct without dates — fall back to standard parser

    n = len(date_values)

    # ── Step 3: Pair dates with content triplets (org, role, resp) ────────────
    # Expect n * k content groups where k ≥ 2 (at least org + role).
    # k is determined by dividing total content groups by n.
    k = max(2, round(len(content_groups) / n)) if n else 2

    experience = []
    for i in range(n):
        chunk = content_groups[i * k: (i + 1) * k]
        if not chunk:
            break

        org  = chunk[0] if len(chunk) > 0 else ""
        role = chunk[1] if len(chunk) > 1 else ""
        # Remaining groups as responsibility bullets
        raw_resp = chunk[2:] if len(chunk) > 2 else []
        bullets = []
        for resp in raw_resp:
            for part in re.split(r'[,;]', resp):
                part = part.strip().lstrip("•·▸►▪-– ")
                if part and len(part) > 5:
                    bullets.append(part)
        bullets = bullets[:6]

        # If we only got 1 group, it's likely the org (role unknown)
        if not role:
            role, org = org, ""

        if org or role:
            experience.append({
                "role":    role,
                "company": org,
                "period":  date_values[i],
                "bullets": bullets,
            })

    return experience


def parse_experience(exp_text: str) -> list:
    """
    Robust experience parser handling three common PDF/DOCX formats:

      Format A  — date on same line as role (and often company):
                    "Software Engineer, Google  Jan 2023 – Present"
      Format B  — date on its own line (3-line header):
                    "Software Engineer"       (line N-2)
                    "Acme Corp"               (line N-1)
                    "2023 – Present"          (date boundary, line N)
      Format C  — year in parentheses, no dash range (Additional-Experience style):
                    "Office Manager, Visas USA (2022)"
                    "Lawyer, TozziniFreire Advogados – Contributed ... (2022)"

    Strategy
    ────────
    1. Scan every non-bullet line for date boundaries:
         a. Check the line ALONE for DATE_RANGE_RE.
         b. Merge with next line ONLY to catch a date that wraps to the next line;
            the match MUST start within the current line's portion — never in the
            next line. This prevents the last bullet of role N from being tagged as
            a boundary because role N+1's date bleeds into the merged string.
         c. Also flag lines ending with (YYYY) or (YYYY–YYYY) as boundaries
            (Format C "Additional Experience" entries).
    2. For each boundary determine role / company / period:
         • Format A: meaningful text before/around the date → extract role and
           company by splitting at the last ", " (company is the short suffix after).
         • Format B: no text on the date line → backward-scan 2 lines for title/co.
         • Format C: year in parens at end → role is everything before the paren.
    3. Track header_indices (lines consumed by each role's header). When collecting
       bullets for role N, skip any line owned by role N+1's header.
    4. Fallback: if no boundaries, use blank-line block splitting.

    Format T  — multi-column HTML/PDF table (column-first pdfminer extraction):
                  "Duration  Organization  Designation/Role  Responsibilities"
                  [all dates grouped]  [org/role/resp per row]
                Handled by _parse_table_experience() before the main logic.
    """
    if not exp_text:
        return []

    lines = [l.rstrip() for l in exp_text.splitlines()]

    # ── Format T: multi-column table ─────────────────────────────────────────
    if _looks_like_experience_table(lines):
        table_result = _parse_table_experience(lines)
        if table_result:
            return table_result

    BULLET_CHARS = ("•", "·", "▸", "►", "▪", "-", "–")
    # Supplementary: lone year in parens, e.g.  "(2022)" or "(2018–2019)"
    PAREN_YEAR_RE = re.compile(r'\(\s*(\d{4})\s*(?:[–\-]\s*(\d{4}|\w+))?\s*\)\s*$')

    # lines was already built above (reuse it)
    experience = []

    # ── Step 1: Mark date-line boundaries ────────────────────────────────────
    is_boundary   = [False] * len(lines)
    paren_year    = {}   # i → "YYYY" or "YYYY–YYYY" for Format C entries

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(BULLET_CHARS):
            continue

        # (a) Direct match on this line
        m = DATE_RANGE_RE.search(stripped)
        if m:
            is_boundary[i] = True
            continue

        # (b) Merge with next line — ONLY mark if match starts within stripped
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not nxt.startswith(BULLET_CHARS):
                merged = stripped + " " + nxt
                m2 = DATE_RANGE_RE.search(merged)
                if m2 and m2.start() < len(stripped):
                    # Date begins in the current line → legitimate wrap boundary
                    is_boundary[i] = True
                    continue

        # (c) Format C: line ends with (YYYY) or (YYYY–YYYY)
        pm = PAREN_YEAR_RE.search(stripped)
        if pm:
            yr_start = pm.group(1)
            yr_end   = pm.group(2) or ""
            paren_year[i] = f"{yr_start}–{yr_end}" if yr_end else yr_start
            is_boundary[i] = True

    boundary_indices = [i for i, b in enumerate(is_boundary) if b]

    # ── Fallback: no date boundaries → blank-line block splitting ────────────
    if not boundary_indices:
        for block in re.split(r"\n{2,}", exp_text):
            blines = [l.strip() for l in block.splitlines() if l.strip()]
            if not blines:
                continue
            role, company, period, bullets = blines[0], "", "", []
            for bl in blines[1:]:
                dm = DATE_RANGE_RE.search(bl)
                if dm and not period:
                    period = dm.group().strip()
                    candidate = re.sub(DATE_RANGE_RE, "", bl).strip().rstrip("–-|·,").strip()
                    if candidate:
                        company = candidate
                    continue
                bullet = bl.lstrip("•·▸►▪-– ").strip()
                if bullet and len(bullet) > 10:
                    bullets.append(bullet)
            if role:
                experience.append({"role": role[:150], "company": company[:120],
                                   "period": period, "bullets": bullets[:6]})
        return experience

    # ── Step 2: Extract role / company / period for each boundary ────────────
    role_records = []

    def _split_role_company(text: str):
        """
        Split 'Role Title, Company Name' into (role, company).
        Only splits if the suffix after the LAST comma is a plausible company
        name: short (≤ 50 chars) and contains no ' & ' (which signals a list
        of specialisations, not a company name).
        Returns (text, "") if no clean split is found.
        """
        parts = text.rsplit(", ", 1)
        if len(parts) == 2:
            suffix = parts[1].strip()
            # Reject if it looks like a list of specialisations
            if len(suffix) <= 50 and " & " not in suffix and len(suffix) > 2:
                return parts[0].strip(), suffix
        return text, ""

    for idx, bi in enumerate(boundary_indices):
        prev_bi = boundary_indices[idx - 1] if idx > 0 else -1

        date_str = lines[bi].strip()
        header_indices = {bi}

        # ── Format C: lone year in parens ────────────────────────────────────
        if bi in paren_year:
            period = paren_year[bi]
            # Role is everything before the paren block, trimmed
            role_raw = PAREN_YEAR_RE.sub("", date_str).strip().rstrip("–-|·, ")
            role, company = _split_role_company(role_raw)
            role_records.append({
                "role": role, "company": company, "period": period,
                "bi": bi, "header_indices": header_indices,
            })
            continue

        # Pull period from the date match
        dm = DATE_RANGE_RE.search(date_str)
        period     = dm.group().strip() if dm else date_str
        before_date = re.sub(DATE_RANGE_RE, "", date_str).strip().rstrip("–-|·,").strip()

        # Treat pure date-artifact fragments (e.g. "/ 2011", "()", ":", ": ") as empty
        if before_date and re.match(r'^[:/\d\s–\-—()]+$', before_date):
            before_date = ""
        # Remove trailing empty parentheses left after date extraction, e.g. "… losses ()"
        before_date = re.sub(r'\s*\(\s*\)\s*$', '', before_date).rstrip("–-|·, ").strip()

        if before_date:
            # ── Format A: role (and company) on the same line as the date ────
            role, company_inline = _split_role_company(before_date)
            company = company_inline

            if not company:
                # No inline company — look one non-empty line ABOVE the boundary.
                # Accept it as the company only if:
                #   • not a bullet char
                #   • does not contain a date (avoids picking up previous role's date line)
                #   • does NOT end with '.' (bullet/sentence terminator)
                #   • short enough to be a name (≤ 70 chars)
                # This correctly picks up "Clase Azul Mexico, Guadalajara, Jalisco"
                # (directly above the role line in Juan Carlos's CV) while rejecting
                # long achievement bullets that end with a period.
                j = bi - 1
                while j > prev_bi and not lines[j].strip():
                    j -= 1
                if j > prev_bi:
                    potential = lines[j].strip()
                    if (potential
                            and not potential.startswith(BULLET_CHARS)
                            and not DATE_RANGE_RE.search(potential)
                            and not potential.endswith(".")
                            and len(potential) <= 70):
                        company = potential
                        header_indices.add(j)

        else:
            # ── Format B / D: date is alone on the line ───────────────────────
            #
            # Format D (consulting-style CVs, e.g. TCS/Accenture project sheets):
            #   Project: <name>                   ← line above boundary
            #   : Mar 2022 Till Date              ← boundary (before_date = "")
            #   Client: <company>                 ← bi+1
            #   Implementation Partner: <firm>    ← bi+2
            #   Role: <title>                     ← bi+3
            #   Responsibilities:                 ← bi+4
            #
            # Detect Format D by scanning the next few lines after the date boundary
            # for a "Role:" label.  If found, also extract "Client:" as company.
            # All labeled lines (Project/Client/Implementation Partner/Role/
            # Responsibilities/Environment) are added to header_indices so they
            # are excluded from the bullet collection loop in Step 3.

            _next_search_end = (
                boundary_indices[idx + 1] if idx + 1 < len(boundary_indices) else len(lines)
            )
            _ROLE_LABEL_RE   = re.compile(r'^Role\s*:\s*', re.IGNORECASE)
            _CLIENT_LABEL_RE = re.compile(r'^Client\s*:\s*', re.IGNORECASE)
            _SKIP_AHEAD_RE   = re.compile(
                r'^(project|implementation\s+partner|responsibilities|environment'
                r'|role|client)\s*:',
                re.IGNORECASE,
            )

            role_d    = ""
            company_d = ""

            for k in range(bi + 1, min(bi + 10, _next_search_end)):
                s = lines[k].strip()
                if not s:
                    continue
                if _ROLE_LABEL_RE.match(s) and not role_d:
                    role_d = _ROLE_LABEL_RE.sub("", s).strip().rstrip(".").strip()
                    header_indices.add(k)
                elif _CLIENT_LABEL_RE.match(s) and not company_d:
                    company_d = _CLIENT_LABEL_RE.sub("", s).strip()
                    header_indices.add(k)
                elif _SKIP_AHEAD_RE.match(s):
                    # Project:, Implementation Partner:, Responsibilities:, Environment:
                    header_indices.add(k)

            # Also skip the "Project:" line directly above the boundary
            # (it belongs to the current record's header, not a previous bullet)
            _j = bi - 1
            while _j > prev_bi and not lines[_j].strip():
                _j -= 1
            if _j > prev_bi:
                _above = lines[_j].strip()
                if re.match(r'^Project\s*:', _above, re.IGNORECASE):
                    header_indices.add(_j)

            if role_d:
                # ── Format D confirmed ────────────────────────────────────────
                role    = role_d
                company = company_d
            else:
                # ── Format B: look backward for role title 2 lines above ──────
                title_candidates = []
                j = bi - 1
                while j > prev_bi and len(title_candidates) < 2:
                    s = lines[j].strip()
                    if s:
                        if s.startswith(BULLET_CHARS):
                            break
                        title_candidates.insert(0, (j, s))
                    j -= 1

                if len(title_candidates) >= 2:
                    role    = title_candidates[-2][1].rstrip("–-|·,").strip()
                    company = title_candidates[-1][1].strip()
                    header_indices.update({title_candidates[-2][0], title_candidates[-1][0]})
                elif title_candidates:
                    role    = title_candidates[-1][1].rstrip("–-|·,").strip()
                    company = ""
                    header_indices.add(title_candidates[-1][0])
                else:
                    role    = ""
                    company = ""

        role_records.append({
            "role": role, "company": company, "period": period,
            "bi": bi, "header_indices": header_indices,
        })

    # ── Step 3: Collect bullets for each role ────────────────────────────────
    # Lines to treat as sub-section separators (skip them, don't include as bullets)
    SUBSECTION_RE = re.compile(
        r'^(additional\s+experience|other\s+experience|earlier\s+experience'
        r'|earlier\s+roles?|previous\s+roles?|other\s+roles?)$',
        re.IGNORECASE
    )
    # Consulting-format label lines that should never appear as bullets
    # (Format D: Project:, Client:, Role:, Responsibilities:, Environment:, etc.)
    CONSULTING_LABEL_RE = re.compile(
        r'^(project|client|implementation\s+partner|role|responsibilities'
        r'|environment|technology|technologies)\s*:',
        re.IGNORECASE
    )

    for idx, rec in enumerate(role_records):
        bi      = rec["bi"]
        next_bi = boundary_indices[idx + 1] if idx + 1 < len(boundary_indices) else len(lines)
        next_header = role_records[idx + 1]["header_indices"] if idx + 1 < len(role_records) else set()

        bullets = []
        for k in range(bi + 1, next_bi):
            # Skip lines claimed by the next role's header (Format A/B backward scan)
            if k in next_header:
                continue
            # Skip lines claimed by the current role's own header (Format D labels)
            if k in rec["header_indices"]:
                continue
            s = lines[k].strip()
            if not s:
                continue
            # Skip consulting-format labeled lines (Project:, Client:, Role:, etc.)
            if CONSULTING_LABEL_RE.match(s):
                continue
            # Skip all-caps sub-section separators
            if (s == s.upper() and len(s) < 50
                    and re.search(r'[A-Z]', s)
                    and not s.startswith(BULLET_CHARS)):
                continue
            # Skip known sub-section labels (title-case)
            if SUBSECTION_RE.match(s):
                continue
            bullet = s.lstrip("•·▸►▪-– ").strip()
            if bullet and len(bullet) > 8:
                bullets.append(bullet)

        clean_role = rec["role"].strip().rstrip("–-|·, ").strip()
        # Skip entries that look like education — they belong in the education section.
        # Only check at the START of the role string to avoid false positives like
        # "Immigration Associates LLC" matching the word "associate".
        _DEGREE_START_RE = re.compile(
            r'(?i)^(bachelor|master|ph\.?d\.?|m\.?sc?\.?|b\.?sc?\.?|mba'
            r'|post.?grad|associate\s+degree|diploma|licenci)',
        )
        if clean_role and _DEGREE_START_RE.match(clean_role):
            continue
        if clean_role:
            experience.append({
                "role":    clean_role[:150],
                "company": rec["company"][:120],
                "period":  rec["period"],
                "bullets": bullets[:6],
            })

    return experience  # no cap

# ── Certifications Parsing ────────────────────────────────────────────────────

def parse_certifications(text: str) -> list:
    if not text:
        return []
    items = []
    for line in text.splitlines():
        # Certifications are often pipe-delimited on a single line:
        # "AWS Certified Solutions Architect | Google Cloud Professional | ..."
        parts = re.split(r"\s*\|\s*", line)
        for part in parts:
            part = part.strip().strip("•·▸►▪-–")
            if part and len(part) > 4:
                items.append(part)
    return items  # no cap

# ── Projects Parsing ──────────────────────────────────────────────────────────

_CLIENT_BLOCK_RE = re.compile(r'(?i)^client\s*:', re.MULTILINE)

# Labels that appear as stand-alone lines (empty value) in column-first table extraction
_PROJ_LABEL_ONLY_RE = re.compile(
    r'(?i)^(&\s*)?(client|domain|role|team\s+size|duration|testing\s+methodology|'
    r'details?|project\s*&?\s*client|project)\s*[:\-–]?\s*$'
)
_ROLE_WORDS_RE = re.compile(
    r'(?i)\b(engineer|lead|manager|analyst|test|qa|developer|architect|senior|junior|specialist)\b'
)
_DOMAIN_WORDS = frozenset({
    'financial', 'healthcare', 'facility', 'maintenance', 'property', 'real estate',
    'management', 'services', 'insurance', 'banking', 'retail', 'logistics',
    'technology', 'telecom', 'manufacturing', 'automotive', 'aerospace',
})
# Lines whose first word is a common action/gerund verb are bullet continuations,
# not org names — even when they start with a capital letter and lack a trailing period.
_ACTION_START_RE = re.compile(
    r'^(Perform|Involv|Participat|Prepar|Prioritiz|Analyz|Conduct|Support|'
    r'Ensur|Monitor|Coordinat|Proactively|Responsib|Develop|Creat|Manag|'
    r'Timely|Logging|Interact|Resolv|Design|Execut|Validat|Deliver|'
    r'Regular|Implement|Review|Follow|Tracking|Reporting|Submission|'
    r'Adhering|Re-order|Vendor|Lead\b|Worked|Supported|Helped)',
    re.IGNORECASE
)


def _classify_proj_line(line: str):
    """Return ('empty'|'label'|'rar'|'date'|'bullet'|'numeric'|'method'|'desc'|'role'|'domain'|'cont'|'meta', value)."""
    s = line.strip()
    if not s:
        return 'empty', s
    if _PROJ_LABEL_ONLY_RE.match(s):
        return 'label', s
    if re.match(r'(?i)^role\s*&?\s*responsibilities?\s*[:\-–]?\s*$', s):
        return 'rar', s
    dm = DATE_RANGE_RE.search(s)
    if dm and len(s) < 60:
        return 'date', dm.group().strip()
    if s and s[0] in "•·▸►▪":
        return 'bullet', s.lstrip("•·▸►▪-– \t").strip()
    if re.match(r'^\d+$', s):
        return 'numeric', s
    if re.match(r'(?i)^(agile|waterfall|scrum|kanban|v-model|hybrid)\s*$', s):
        return 'method', s
    if len(s) > 80:
        return 'desc', s
    # Lines starting with lowercase → sentence continuation (not an org name)
    if s[0].islower():
        return 'cont', s
    # Lines ending with "." are sentences/continuations, NOT org names
    if s.endswith('.') and len(s) > 10:
        return 'cont', s
    # Lines starting with a common action/gerund verb → bullet continuation
    if _ACTION_START_RE.match(s):
        return 'cont', s
    if _ROLE_WORDS_RE.search(s) and len(s) < 60:
        return 'role', s
    if any(w in s.lower() for w in _DOMAIN_WORDS) and len(s) < 70:
        return 'domain', s
    return 'meta', s


def _parse_client_block_projects(text: str) -> list:
    """
    Parse consulting/QA-style project sections using a state machine.

    The PDF table is extracted column-first, so labels and values appear
    interleaved in non-trivial ways.  Instead of trying to match labels to values,
    we drive a state machine through every line:

      STATE_META    — collecting metadata for the current project
      STATE_BULLETS — collecting bullet responsibilities

    Transitions:
      META → BULLETS  : when "Role & Responsibilities" heading is seen
      BULLETS → META  : when a new [meta] (org name) line appears after bullets,
                        which signals the start of a new project

    A lone "•" bullet char followed by a [meta]-classified line means the next
    line is actually a bullet continuation (the "•" and its text were split across
    lines in the PDF).
    """
    STATE_META    = "meta"
    STATE_BULLETS = "bullets"

    items: list[dict] = []
    cur = {"org": "", "role": "", "domain": "", "date": "", "description": "", "bullets": []}
    state = STATE_META
    prev_lone_bullet = False   # True when previous non-empty line was a bare "•"

    def _flush():
        if cur.get("org") or cur.get("date") or cur.get("bullets"):
            title = cur["org"] or (cur.get("description") or "")[:60] or "Project"
            items.append({
                "title":       title,
                "client":      cur["org"],
                "role":        cur["role"],
                "period":      cur["date"],
                "description": (cur.get("description") or "")[:400],
                "bullets":     cur["bullets"][:6],
            })

    def _reset() -> dict:
        return {"org": "", "role": "", "domain": "", "date": "", "description": "", "bullets": []}

    for line in text.splitlines():
        kind, val = _classify_proj_line(line)

        # A lone "•" char produces kind='bullet' with val='' — track it
        is_lone_bullet = (kind == 'bullet' and not val)
        if is_lone_bullet:
            prev_lone_bullet = True
            continue   # nothing to store yet

        # If previous non-empty line was "•" and this line looks like an org name,
        # it's actually the bullet text (the PDF split "• Text" across two lines).
        if prev_lone_bullet and kind == 'meta':
            kind = 'bullet'   # re-classify as bullet continuation
        prev_lone_bullet = False

        # Skip non-content line types
        if kind in ('empty', 'numeric', 'method', 'label'):
            continue

        # "Role & Responsibilities" → switch to bullet-collection mode
        if kind == 'rar':
            state = STATE_BULLETS
            continue

        # ── STATE: collecting project metadata ────────────────────────────
        if state == STATE_META:
            if kind == 'date' and not cur['date']:
                cur['date'] = val
            elif kind == 'desc' and not cur['description']:
                cur['description'] = val
            elif kind == 'role' and not cur['role']:
                cur['role'] = val
            elif kind == 'domain' and not cur['domain']:
                cur['domain'] = val
            elif kind == 'meta' and not cur['org']:
                cur['org'] = val
            elif kind in ('bullet', 'cont') and val:
                # Bullet appearing before R&R — this project's R&R section follows
                # (shouldn't happen, but handle gracefully)
                state = STATE_BULLETS
                if len(val) > 5:
                    cur['bullets'].append(val)

        # ── STATE: collecting project bullets ─────────────────────────────
        elif state == STATE_BULLETS:
            if kind in ('bullet', 'cont') and val and len(val) > 5:
                cur['bullets'].append(val)
            elif kind == 'meta':
                # New org-name line after bullets → new project starts
                _flush()
                cur   = _reset()
                cur['org'] = val
                state = STATE_META
            elif kind == 'date' and not cur['date']:
                # A date appearing in the bullets section belongs to this project
                cur['date'] = val
            # desc/role/domain in bullet state → skip (template artifacts)

    _flush()   # save the last project
    return items


def parse_projects(text: str) -> list:
    if not text:
        return []

    # ── Consulting/project-sheet format: structured Client: blocks ────────────
    # Detected when "Client:" appears as a labeled line (at least twice for
    # multiple projects, or once if the section is substantial).
    client_matches = len(_CLIENT_BLOCK_RE.findall(text))
    if client_matches >= 1:
        result = _parse_client_block_projects(text)
        if result:
            return result

    # ── Standard format: double-newline-separated project blocks ─────────────
    items = []
    blocks = re.split(r"\n{2,}", text)
    # Filter out pure-label blocks (e.g. "Client:\n\nDomain:\n\n") with no real content
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        first = lines[0]
        # Skip blocks where first line is a bare label (e.g. "Client:" alone)
        if re.match(r'(?i)^(client|domain|role|duration|testing\s+methodology)\s*:?\s*$', first):
            continue
        items.append({"title": first, "description": " ".join(lines[1:])[:300]})
    return items  # no cap

# ── Awards Parsing ────────────────────────────────────────────────────────────

def parse_awards(text: str) -> list:
    if not text:
        return []
    items = []
    for line in text.splitlines():
        line = line.strip().strip("•·▸►▪-–")
        if line and len(line) > 4:
            items.append(line)
    return items  # no cap

# ── Stats Generation ──────────────────────────────────────────────────────────

def generate_stats(experience: list, languages: list) -> list:
    stats = []

    # Years of experience
    years = 0
    for exp in experience:
        m = re.findall(r"\d{4}", exp.get("period", ""))
        if len(m) >= 2:
            start = int(m[0])
            end_str = m[1]
            end = datetime.now().year if end_str.lower() in ("present", "current", "now") else int(end_str)
            years += max(0, end - start)
    if years > 0:
        stats.append({"number": f"{years}+", "label": "Years of professional experience"})

    # Number of roles
    if experience:
        stats.append({"number": str(len(experience)), "label": f"Role{'s' if len(experience) > 1 else ''} across different companies"})

    # Languages
    if languages:
        stats.append({"number": str(len(languages)), "label": f"Language{'s' if len(languages) > 1 else ''} spoken"})

    return stats

# ── LLM-based Extraction Fallback ────────────────────────────────────────────

def llm_extract_cv(text: str) -> dict | None:
    """
    Use Claude API (claude-3-haiku) to extract a complete, structured CV dict
    from raw text.  Only called when:
      • ANTHROPIC_API_KEY is set in the environment
      • The regex parser produced < 2 experience entries for a substantial CV

    Returns a dict in the same shape as parse_cv(), or None on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    try:
        import anthropic  # already validated available at module level

        client = anthropic.Anthropic(api_key=api_key)

        system_prompt = (
            "You are an expert HR data extractor. Given raw CV text, return a single "
            "valid JSON object — no prose, no markdown, no code fences. The schema is:\n"
            '{\n'
            '  "candidate_name": "string",\n'
            '  "candidate_initials": "string (2 chars)",\n'
            '  "candidate_title": "string",\n'
            '  "candidate_email": "string",\n'
            '  "candidate_phone": "string",\n'
            '  "candidate_location": "string",\n'
            '  "candidate_linkedin": "string",\n'
            '  "summary": "string (2-4 sentence professional summary)",\n'
            '  "experience": [\n'
            '    {"role": "string", "company": "string", "period": "string", '
            '"bullets": ["string", ...]}\n'
            '  ],\n'
            '  "skills": ["string", ...],\n'
            '  "languages": [{"name": "string", "level": "string", "percent": 0-100}],\n'
            '  "education": [{"degree": "string", "institution": "string"}],\n'
            '  "certifications": ["string", ...],\n'
            '  "projects": [],\n'
            '  "awards": []\n'
            '}\n'
            "Rules:\n"
            "- Extract ALL work experience entries — never omit any role.\n"
            "- For consulting CVs with Project/Client/Role labels, use 'Role' as the role "
            "and 'Client' as the company.\n"
            "- Include up to 6 bullet points per role (pick the most impactful ones).\n"
            "- If no summary exists in the CV, leave the summary field as an empty string.\n"
            "- Return ONLY the JSON object — no other text."
        )

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract all CV data from the following text and return as JSON:\n\n"
                        + text
                    ),
                }
            ],
            system=system_prompt,
        )

        raw = response.content[0].text.strip()
        # Strip accidental code fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)

        # Ensure required fields and types
        data.setdefault("candidate_name", "Candidate")
        data.setdefault("candidate_initials",
                        "".join(w[0].upper() for w in data["candidate_name"].split()[:2]))
        data.setdefault("candidate_title", "")
        data.setdefault("candidate_email", "")
        data.setdefault("candidate_phone", "")
        data.setdefault("candidate_location", "")
        data.setdefault("candidate_linkedin", "")
        data.setdefault("summary", "")
        data.setdefault("experience", [])
        data.setdefault("skills", [])
        data.setdefault("languages", [])
        data.setdefault("education", [])
        data.setdefault("certifications", [])
        data.setdefault("projects", [])
        data.setdefault("awards", [])
        data.setdefault("volunteer", "")
        data.setdefault("stats", generate_stats(data["experience"], data["languages"]))

        print(f"   🤖 LLM extraction used — {len(data['experience'])} roles found")
        return data

    except Exception as exc:
        print(f"   ⚠️  LLM extraction failed: {exc}")
        return None


# ── Bullet-section rescue ─────────────────────────────────────────────────────
#
# Two CV formats drop section headings into the wrong bucket:
#
# Pattern A — bullet-prefixed headings (DOCX with bullets):
#   "▸ EDUCATION  Bachelor of Technology in Computer Science"
#   "▸ CERTIFICATIONS  AWS Certified Solutions Architect"
#   split_sections() sees "▸ …" → matches nothing → lands in active section.
#
# Pattern B — ALL-CAPS inline headings (single-column DOCX, no blank separator):
#   "EDUCATION Bachelor of Laws (LL.B.) Universidade do Vale do Taquari – Univates, 2018"
#   "CERTIFICATIONS Software Architect | EF SET English C2 Proficient | ..."
#   "LANGUAGES English (Native/Bilingual) • Portuguese (Native) ..."
#   split_sections() matches the keyword but Guard 1 (len > 60) rejects the line
#   because heading + content is all on one line.
#
# rescue_bullet_sections() scans every section's text for both patterns and
# re-routes the content into the correct destination section.

# Pattern A — optional bullet prefix (case-insensitive)
_BULLET_SECTION_RE = re.compile(
    r'^[•·▸►▪\-–▶]\s*'
    r'(education|certific\w*|licens\w*|language|qualification|degree|awards?|honors?)\b'
    r'[\s:]*(.*)$',
    re.IGNORECASE,
)

# Pattern B — ALL-CAPS keyword only (case-SENSITIVE to avoid false positives)
_ALLCAPS_SECTION_RE = re.compile(
    r'^(EDUCATION|CERTIFICATIONS?|LANGUAGES?|LICENS\w*|AWARDS?|HONORS?)\b\s*(.*)$',
)

_SECTION_KEY_MAP = {
    "edu":    "education",
    "qual":   "education",
    "deg":    "education",
    "certif": "certifications",
    "licens": "certifications",
    "lang":   "languages",
    "award":  "awards",
    "honor":  "awards",
}

def _bullet_section_target(keyword: str) -> str:
    kl = keyword.lower()
    for prefix, key in _SECTION_KEY_MAP.items():
        if kl.startswith(prefix):
            return key
    return ""

def rescue_bullet_sections(sections: dict) -> dict:
    """
    Re-routes bullet-prefixed or ALL-CAPS inline section headings from whichever
    section they landed in to the correct destination section.
    Safe to call on every CV — only modifies sections that actually contain such lines.
    """
    for src_key in list(sections.keys()):
        if src_key.startswith("_"):
            continue
        src_text = sections[src_key]
        if not src_text:
            continue
        src_lines = src_text.splitlines()
        kept = []
        i = 0
        while i < len(src_lines):
            line = src_lines[i]
            stripped = line.strip()

            # Try Pattern A (bullet-prefix) first, then Pattern B (ALL-CAPS)
            m = _BULLET_SECTION_RE.match(stripped) or _ALLCAPS_SECTION_RE.match(stripped)
            if m:
                target = _bullet_section_target(m.group(1))
                if target:
                    inline = m.group(2).strip()
                    # Collect subsequent non-heading lines as this section's content
                    rescued = [inline] if inline else []
                    i += 1
                    while i < len(src_lines):
                        nxt = src_lines[i].strip()
                        if (_BULLET_SECTION_RE.match(nxt) or _ALLCAPS_SECTION_RE.match(nxt)):
                            break
                        rescued.append(src_lines[i])
                        i += 1
                    chunk = "\n".join(rescued).strip()
                    if chunk:
                        existing = sections.get(target, "")
                        sections[target] = (existing + "\n\n" + chunk).strip() if existing else chunk
                    continue  # don't add these lines to kept[]
            kept.append(line)
            i += 1
        sections[src_key] = "\n".join(kept)
    return sections


# ── Full Parse ────────────────────────────────────────────────────────────────

def parse_cv(text: str) -> dict:
    sections        = split_sections(text)
    sections        = rescue_bullet_sections(sections)   # rescue "▸ EDUCATION …" lines
    header          = parse_header(sections.get("_header", ""))
    skills          = parse_skills(sections.get("skills", ""))
    languages       = parse_languages(sections.get("languages", ""))
    education       = parse_education(sections.get("education", ""))
    experience      = parse_experience(sections.get("experience", ""))
    certifications  = parse_certifications(sections.get("certifications", ""))
    projects        = parse_projects(sections.get("projects", ""))
    awards          = parse_awards(sections.get("awards", ""))
    volunteer       = sections.get("volunteer", "").strip()
    overflow_raw    = sections.get("_overflow", "")
    overflow        = overflow_raw.strip() if isinstance(overflow_raw, str) else ""
    summary         = sections.get("summary", "").strip()
    # Clean up decorative bullet chars that came from Wingdings/Symbol PUA substitution
    # (e.g. "\uf0b7" → "•" used as separators/decorators in the original PDF).
    # A summary is a flowing paragraph — bullet chars should never appear in it.
    summary = re.sub(r'^[•\s]+', '', summary)          # strip leading bullets/spaces
    summary = re.sub(r'[•]+', ' ', summary)            # replace any remaining bullets with space
    summary = re.sub(r'  +', ' ', summary).strip()     # normalise multiple spaces

    # ── Tech-skills rescue: "Languages" section containing programming/tool names ──
    # Some CVs (e.g. Indian consulting CVs) list tech tools under a heading called
    # "Languages". parse_languages() filters those out.  If that left languages empty
    # but the raw section text is substantial, treat it as additional skills instead.
    lang_raw = sections.get("languages", "").strip()
    if not languages and lang_raw and len(lang_raw) > 30:
        rescued_skills = parse_skills(lang_raw)
        if rescued_skills:
            # Merge, dedup (preserve order)
            seen = {s.lower() for s in skills}
            for s in rescued_skills:
                if s.lower() not in seen:
                    skills.append(s)
                    seen.add(s.lower())

    # ── Skill inference: extract tool/software names from experience bullets ──────
    # Only runs when no explicit skills section was found.
    # Strategy: look for capitalized multi-word proper nouns that look like software,
    # platforms, or methodologies — NOT verbs, NOT generic role words.
    # Conservative: it's better to show a blank skills section than to show junk.
    _ROLE_WORDS = {
        "executive","manager","assistant","specialist","officer","director",
        "head","lead","senior","junior","associate","coordinator","analyst",
        "advisor","consultant","representative","administrator","supervisor",
    }
    _VERB_PATTERN = re.compile(
        r'^(managed|led|developed|created|designed|delivered|improved|built|'
        r'trained|coached|achieved|exceeded|analyzed|implemented|coordinated|'
        r'supported|executed|drove|generated|increased|reduced|oversaw|handled|'
        r'maintained|established|prepared|provided|collaborated|worked|'
        r'facilitated|identified|ensured|conducted)\b', re.IGNORECASE
    )
    if not skills and experience:
        inferred = []
        seen = set()
        for exp_item in experience[:4]:
            for bullet in exp_item.get("bullets", [])[:4]:
                # Extract parenthesised tool lists: "CRM platforms (HubSpot, GoHighLevel)"
                for group in re.findall(r'\(([^)]{5,60})\)', bullet):
                    for item in re.split(r'[,;]', group):
                        item = item.strip()
                        if 3 < len(item) < 28 and item.lower() not in seen:
                            seen.add(item.lower())
                            inferred.append(item)
        skills = inferred[:15]

    stats           = generate_stats(experience, languages)

    # ── Overflow rescue: "ORG as ROLE\n• bullets" blocks ────────────────────
    # Some multi-column table CVs (e.g. Venkata Satish) have all detailed
    # experience narratives routed to _overflow because org names in ALL-CAPS
    # trigger the overflow redirect.  Detect and rescue them here.
    _ORG_AS_ROLE_RE = re.compile(
        r'^(.+?)\s+as\s+(.+?)$',
        re.IGNORECASE | re.MULTILINE
    )
    # Count "ORG as ROLE" matches in overflow to gauge quality
    _oras_count = len(_ORG_AS_ROLE_RE.findall(overflow)) if overflow else 0
    # Malformed when most entries have neither a period nor a company name
    # (typical of multi-column table extraction garbage)
    _no_period_no_company = sum(
        1 for e in experience if not e.get('period') and not e.get('company')
    )
    _exp_malformed = (
        not experience or
        _no_period_no_company >= max(1, len(experience) // 2)
    )
    if overflow and _oras_count >= 2 and (len(experience) < 4 or _exp_malformed):
        # Extract "ORG as ROLE\n• bullets" blocks
        # Split on lines that match "ORG as ROLE" (non-bullet, non-empty)
        overflow_lines = overflow.splitlines()
        overflow_exp = []
        cur_org = cur_role = ""
        cur_bullets: list[str] = []

        # Also extract dates from the overflow header (grouped date block)
        # Dates appear before the "ORG as ROLE" narrative blocks.
        # Venkata-style CVs use "Aug2023to\nMar2025" (month+year split across two lines
        # AND no space between month abbreviation and year).
        # Fix: collapse single newlines to spaces before date scanning so that
        # "Aug2023to \nMar2025" becomes "Aug2023to  Mar2025" in one chunk.
        # _OVERFLOW_DATE_RE uses [a-z]* (not \w*) so digits after month aren't consumed.
        _OVERFLOW_DATE_RE = re.compile(
            r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}'
            r'\s*(?:[-–—]+|to|till)\s*'
            r'(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}'
            r'|present|current|now|date)',
            re.IGNORECASE
        )
        # Collapse single newlines → space so split date ranges join up; keep \n\n as group seps
        _overflow_for_dates = re.sub(r'(?<!\n)\n(?!\n)', ' ', overflow)
        overflow_dates: list[str] = []
        for chunk in re.split(r'\n{2,}', _overflow_for_dates):
            chunk = chunk.strip()
            if not chunk or len(chunk) > 70:
                continue
            dm = DATE_RANGE_RE.search(chunk) or _OVERFLOW_DATE_RE.search(chunk)
            if dm:
                overflow_dates.append(dm.group().strip())

        date_iter = iter(overflow_dates)

        def _flush_overflow_entry():
            if cur_org or cur_role:
                period = next(date_iter, "")
                overflow_exp.append({
                    "role":    cur_role,
                    "company": cur_org,
                    "period":  period,
                    "bullets": cur_bullets[:6],
                })

        BULLET_CHARS_SET = {"•", "·", "▸", "►", "▪", "-", "–"}
        for line in overflow_lines:
            s = line.strip()
            if not s:
                continue
            # Skip date-only lines (already captured above)
            dm = DATE_RANGE_RE.search(s) or _OVERFLOW_DATE_RE.search(s)
            if dm and len(s) < 50:
                continue
            # Skip domain/segment labels (Industrial, Medical, Rail, etc.)
            if re.match(r'^(industrial|medical|rail|automotive|aerospace|heavy\s+engineering|'
                         r'automotive/consumer|automotive/mfg)', s, re.IGNORECASE) and len(s) < 30:
                continue

            # Check for "ORG as ROLE" boundary
            m_ar = re.match(r'^(.+?)\s+as\s+(.+)$', s, re.IGNORECASE)
            if m_ar and not s.startswith(tuple(BULLET_CHARS_SET)):
                potential_org  = m_ar.group(1).strip()
                potential_role = m_ar.group(2).strip()
                # Sanity: both sides should be reasonably short
                if len(potential_org) < 80 and len(potential_role) < 60:
                    _flush_overflow_entry()
                    cur_org  = potential_org
                    cur_role = potential_role
                    cur_bullets = []
                    continue

            # Collect bullets
            if s.startswith(tuple(BULLET_CHARS_SET)):
                bullet = s.lstrip("•·▸►▪-– \t").strip()
                if bullet and len(bullet) > 5:
                    cur_bullets.append(bullet)

        _flush_overflow_entry()  # last entry

        # Use overflow_exp if: (a) original parse was malformed (quality beats count),
        # or (b) overflow produced strictly more entries.
        if overflow_exp and (_exp_malformed or len(overflow_exp) > len(experience)):
            experience = overflow_exp

    # ── Experience quality guard: detect garbage parses ───────────────────────
    # If most experience entries have date fragments as role names (table-format
    # parse failure), treat the parse as failed and trigger LLM fallback.
    _DATE_FRAG_RE = re.compile(r'^\d{4}\s*(till|to|–|-|—)')
    _HEADER_WORDS = {'duration', 'organization', 'designation', 'role', 'responsibilities',
                     'segment', 'domain', 'employer', 'company'}
    def _experience_is_malformed(exp_list: list) -> bool:
        if not exp_list:
            return True
        bad = 0
        for e in exp_list:
            r = e.get('role', '').lower().strip()
            if _DATE_FRAG_RE.match(e.get('role', '')):
                bad += 1
            elif r in _HEADER_WORDS:
                bad += 1
        return bad >= max(1, len(exp_list) // 2)

    # ── LLM fallback: engage when regex extraction is clearly incomplete ──────
    # Trigger when: (a) fewer than 2 experience roles OR parse is clearly
    # malformed (table-format garbage), AND (b) ANTHROPIC_API_KEY is available.
    # The LLM result replaces the entire parsed dict when it finds more roles.
    if (_experience_is_malformed(experience) or len(experience) < 2) and len(text) > 800:
        llm_data = llm_extract_cv(text)
        if llm_data and len(llm_data.get("experience", [])) > len(experience):
            return llm_data

    # Derive title from first experience role if header had none
    if not header["candidate_title"] and experience:
        raw = experience[0]["role"]
        derived = re.sub(DATE_RANGE_RE, "", raw).strip().strip("–-|·").strip()
        # Shorten long role titles to a clean summary
        parts = re.split(r"[|,&]", derived)
        header["candidate_title"] = " · ".join(p.strip() for p in parts[:3] if p.strip())[:100]

    return {
        **header,
        "summary":        summary,
        "skills":         skills,
        "languages":      languages,
        "education":      education,
        "experience":     experience,
        "certifications": certifications,
        "projects":       projects,
        "awards":         awards,
        "volunteer":      volunteer,
        "stats":          stats,
    }

# ── PDF Generation ────────────────────────────────────────────────────────────

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "simpalm_cv_TEMPLATE.html"

def render_pdf(data: dict, output_path: str):
    from jinja2 import Template
    from weasyprint import HTML

    template_src = TEMPLATE_PATH.read_text(encoding="utf-8")
    template     = Template(template_src)
    html_content = template.render(**data)

    HTML(string=html_content, base_url=str(TEMPLATE_PATH.parent)).write_pdf(output_path)
    print(f"✅ PDF written: {output_path}")

# ── Index Update ──────────────────────────────────────────────────────────────

INDEX_PATH = Path(__file__).parent.parent / "index.json"

def update_index(data: dict, pdf_filename: str, source_path: str = ""):
    index = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []

    # Remove existing entry for same candidate
    index = [e for e in index if e.get("name") != data["candidate_name"]]

    entry = {
        "name":      data["candidate_name"],
        "title":     data["candidate_title"],
        "location":  data["candidate_location"],
        "filename":  pdf_filename,
        "processed": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Store the original inbox path so the frontend can selectively re-trigger this CV
    if source_path:
        entry["source_file"] = source_path.replace("\\", "/")

    index.append(entry)
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False))
    print(f"✅ index.json updated ({len(index)} entries)")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Process a CV into a Simpalm-branded PDF")
    parser.add_argument("cv_path", help="Path to the CV file (PDF or DOCX)")
    args = parser.parse_args()

    cv_path = args.cv_path
    if not os.path.exists(cv_path):
        print(f"❌ File not found: {cv_path}")
        sys.exit(1)

    print(f"📄 Processing: {cv_path}")

    # 1. Extract text
    text = extract_text(cv_path)
    print(f"   Extracted {len(text)} characters")

    # 2. Parse structured data
    data = parse_cv(text)
    print(f"   Candidate:      {data['candidate_name']}")
    print(f"   Title:          {data['candidate_title']}")
    print(f"   Skills:         {len(data['skills'])}")
    print(f"   Exp roles:      {len(data['experience'])}")
    print(f"   Certifications: {len(data['certifications'])}")
    print(f"   Projects:       {len(data['projects'])}")
    print(f"   Awards:         {len(data['awards'])}")

    # 3. Build output filename
    safe_name   = re.sub(r"[^\w\s-]", "", data["candidate_name"]).strip()
    pdf_filename = f"{safe_name} - CV Simpalm Staffing.pdf"
    output_path  = Path(__file__).parent.parent / "processed" / pdf_filename

    # 4. Render PDF
    render_pdf(data, str(output_path))

    # 5. Update registry
    update_index(data, pdf_filename, source_path=cv_path)

    print(f"\n🎉 Done! → processed/{pdf_filename}")

if __name__ == "__main__":
    main()
