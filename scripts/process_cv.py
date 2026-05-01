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
    # Strip NUL bytes — some PDFs with ligature glyphs (fi, ff) produce \x00
    # in the extracted text, corrupting words like "financial" → "fi\x00nancial".
    text = text.replace('\x00', '')
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
            alpha_tokens = [t for t in tokens if t and t.isalpha()]
            if all(t.isupper() for t in alpha_tokens):
                # ALL-CAPS spaced: "J U A N   C A R L O S" → "JUAN CARLOS"
                collapsed = re.sub(r'(?<=\w) (?=\w)', '', stripped)
                collapsed = re.sub(r'  +', ' ', collapsed)
            else:
                # Mixed-case spaced: "S a u n d a r y a M i s h r a" → "Saundarya Mishra"
                # Uppercase letter starts a new word, empty token = explicit word break
                result = []
                current = []
                for t in tokens:
                    if not t:  # empty string from multiple consecutive spaces
                        if current:
                            result.append(''.join(current))
                            current = []
                    elif t.isupper() and len(t) == 1 and current:
                        result.append(''.join(current))
                        current = [t]
                    else:
                        current.append(t)
                if current:
                    result.append(''.join(current))
                collapsed = ' '.join(r for r in result if r)
            fixed.append(collapsed)
        else:
            fixed.append(line)
    return '\n'.join(fixed)

def _extract_with_pdfplumber(path: str) -> str:
    """Fallback PDF extractor using pdfplumber (better for multi-column layouts)."""
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=2, y_tolerance=3)
                if t:
                    parts.append(t)
        return '\n'.join(parts)
    except Exception:
        return ""

# Strip sidebar inline-prefix labels that pdfplumber merges onto content lines.
# Example: "Phone Associate Manager" → "Associate Manager"
_INLINE_SIDEBAR_PREFIX_RE = re.compile(
    r'^(phone|email|address|linkedin|github|website|portfolio|contact)\s+',
    re.IGNORECASE,
)

def extract_text_pdf(path: str) -> str:
    from pdfminer.high_level import extract_text
    raw = extract_text(path)
    fixed = fix_char_spacing(raw)
    # Detect single-blob extraction: pdfminer collapsed all text into 1–2 giant lines.
    # This happens with multi-column PDFs where pdfminer can't separate the columns.
    # Fall back to pdfplumber which handles these layouts correctly.
    nonempty = [l for l in fixed.split('\n') if l.strip()]
    if nonempty and len(nonempty) <= 2 and len(nonempty[0]) > 800:
        plumber_raw = _extract_with_pdfplumber(path)
        if plumber_raw:
            # Strip inline sidebar prefixes that pdfplumber merges into content lines
            cleaned_lines = []
            for line in plumber_raw.splitlines():
                cleaned_lines.append(_INLINE_SIDEBAR_PREFIX_RE.sub('', line))
            plumber_raw = '\n'.join(cleaned_lines)
            return fix_char_spacing(plumber_raw)
    return fixed

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
    "summary":        r"(?i)^(professional\s+)?(summary|profile)|about\s+me|objective\b|overview\b|executive\s+summary|career\s+synopsis|career\s+summary|career\s+profile|aims?\s*[&e]?\s*goals?|personal\s+statement",
    "experience":     r"(?i)^(professional\s+|work\s+)?experience|employment|career\s+history|work\s+history",
    "education":      r"(?i)^education|academic|qualification|degree|university|college",
    "skills":         r"(?i)^(\w+\s+)?(skills?|abilities|competenc\w*)|expertise|technologies|tools",
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
            # Exception: "HEADING | inline content" — the "|" separator indicates the
            # content is inline on the same line (e.g. "LANGUAGES | Portuguese – native").
            # In that case check only the heading-keyword length, not the total line.
            m_sec = re.match(pattern, stripped)
            if not m_sec:
                continue
            _heading_end = m_sec.end()
            _after_heading = stripped[_heading_end:].lstrip()
            _has_pipe_content = _after_heading.startswith('|')
            if len(stripped) > 60 and not _has_pipe_content:
                continue
            # Guard 2: reject label lines like "Project: Enterprise Commerce Volume License"
            # where the keyword is immediately followed by ": long content".
            # True headings end with a bare ":" (no content) or have no colon at all.
            # Allow "Computer skills:" (colon + nothing) — that IS a heading.
            remaining_after_kw = stripped[m_sec.end():].lstrip()
            if remaining_after_kw.startswith(":"):
                # Only reject when there is actual content after the colon
                _colon_body = remaining_after_kw.lstrip(":").strip()
                if _colon_body:          # "Keyword: value" → label, skip
                    continue
                # else: "Keyword:" alone → genuine section heading, fall through
            # Guard 3: reject sentence continuations like "Experience with ticketing…"
            # or "Experience in setting up…". A true heading never starts with these
            # relational prepositions immediately after the section keyword.
            _first_word = remaining_after_kw.split()[0].lower() if remaining_after_kw.split() else ""
            if _first_word in {"in", "with", "on", "as", "for", "at", "to", "from"}:
                continue
            current = section
            matched = True
            # If the header line also contains content (e.g. "SKILLS • foo • bar"
            # or "LANGUAGES | Portuguese – native"), capture the inline body.
            if _has_pipe_content:
                # Pipe-separated: "HEADING | content" → take everything after "|"
                after = _after_heading.lstrip('| ').strip()
            else:
                after = re.sub(pattern, "", stripped, count=1,
                               flags=re.IGNORECASE).lstrip(" :–-•·▸►▪")
            if after:
                sections[current].append(after)
            break
        if not matched:
            # Detect unrecognised headings: short, mostly uppercase, no lowercase letters
            if (stripped and len(stripped) <= 40
                    and stripped == stripped.upper()
                    and re.search(r'[A-Z]', stripped)
                    and current not in ("_header",)):
                # Only route to _overflow when the ALL-CAPS text looks like a table
                # column header (ORGANIZATION, DESIGNATION, etc.) or when we are NOT
                # in the experience section.  Plain company names in ALL-CAPS like
                # "ULTRAHUMAN" or "DREAM ON ME" must stay in experience so they are
                # available for boundary-based role extraction.
                _TABLE_COLUMN_WORDS = {
                    'organization', 'designation', 'employer', 'duration',
                    'project', 'client', 'role', 'responsibilities',
                    'domain', 'methodology', 'environment',
                }
                _is_table_col = any(w in stripped.lower() for w in _TABLE_COLUMN_WORDS)
                if current == "experience" and not _is_table_col:
                    # Keep inside experience — it's a company name, not a table header
                    sections[current].append(line)
                else:
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
    # Americas
    "brazil", "brasil", "usa", "united states", "canada", "mexico",
    "colombia", "argentina", "chile", "peru", "venezuela", "ecuador",
    "bolivia", "paraguay", "uruguay", "costa rica", "panama",
    "el salvador", "guatemala", "honduras", "nicaragua",
    "dominican republic", "república dominicana",
    # Cities — Americas
    "brasília", "são paulo", "rio de janeiro", "distrito federal",
    "new york", "miami", "california", "chicago", "los angeles",
    "toronto", "vancouver", "bogota", "lima", "santiago",
    # Europe
    "uk", "united kingdom", "london", "ireland", "dublin",
    "spain", "españa", "madrid", "barcelona", "portugal", "lisbon",
    "poland", "warsaw", "ukraine", "kyiv", "romania", "bucharest",
    # Africa
    "nigeria", "lagos", "abuja", "ghana", "accra",
    "kenya", "nairobi", "south africa", "johannesburg", "cape town",
    # Asia-Pacific
    "india", "mumbai", "delhi", "new delhi", "bangalore", "bengaluru",
    "hyderabad", "pune", "chennai", "kolkata", "noida", "gurgaon",
    "pakistan", "karachi", "lahore", "islamabad",
    "philippines", "manila", "indonesia", "jakarta",
    "bangladesh", "dhaka", "vietnam", "hanoi",
    "australia", "sydney", "melbourne",
    # Note: "remote" and "international" removed — both appear in job titles
    # ("Remote sensing", "International Executive Assistant") causing false positives.
]

# Lines that look like credential chains (e.g. "CPA (USA) | CMA (USA") — reject as location
_CREDENTIAL_LINE_RE = re.compile(
    r'\b(CPA|CMA|CFA|CA|MBA|LLB|PhD|FCA|ACCA|ACA|FCCA|CGA|CIA|EA|CFP|CISA|CGMA)\b'
    r'\s*[\(\[–\-]',
    re.IGNORECASE
)

def parse_header(header_text: str) -> dict:
    lines = [l.strip() for l in header_text.splitlines() if l.strip()]

    # ── Extract name ──────────────────────────────────────────────────────────
    # lines[0] is normally the candidate name.  For multi-column PDFs where
    # pdfminer collapses everything into one long string, lines[0] can be the
    # entire CV text (10 000+ chars).  Guard: accept only lines ≤ 60 chars and
    # that don't start with a digit (dates, section headers) or look like a
    # section keyword.
    _NAME_JUNK_RE = re.compile(
        r'^(curriculum\s+vitae|resume|cv\b|professional\s+(experience|summary|profile)|'
        r'work\s+experience|education|skills|summary|executive\s+(summary|profile)|'
        r'career\s+(synopsis|objective|profile|summary)|professional\s+background|'
        r'strengths?|about\s+me|objective|overview|highlights?|qualifications?|'
        r'contact(ar|o|s)?|contato|roles?|responsibilities|references?|'
        r'personal\s+(data|information|profile|statement)|'
        r'additional\s+(information|details?)|'
        r'language[s]?|certifications?|awards?|achievements?|'
        r'programme?\s+manager|business\s+management|project\s+manager|'
        r'senior\s+\w+|junior\s+\w+|lead\s+\w+|'
        r'argentina|brazil|brasil|india|ireland|pakistan|nigeria|ghana|kenya|'
        r'philippines|indonesia|mexico|colombia|peru|chile|spain|portugal|'
        r'united\s+(states|kingdom)|usa\b|uk\b|uae\b|'
        r'https?://|www\.|linkedin\.com)',
        re.IGNORECASE
    )

    # Lines that look like "City, Country" or "City State" — reject as names
    _LOCATION_LINE_RE = re.compile(
        r'^[\w\s]+,\s*(india|usa|uk|ireland|brazil|brasil|pakistan|nigeria|'
        r'illinois|california|texas|new\s+york|florida|mumbai|bangalore|'
        r'hyderabad|delhi|pune|chennai|dubai|singapore|london|toronto)\s*$',
        re.IGNORECASE
    )

    name = "Candidate"
    for line in lines[:5]:   # check first 5 lines only
        if not line or line[0].isdigit() or _NAME_JUNK_RE.match(line):
            continue
        if _LOCATION_LINE_RE.match(line.strip()):
            continue
        # If the line contains a tab or many consecutive spaces it's probably
        # name + location on the same line (e.g. "RENE BONOMI   São Paulo").
        # Extract the part BEFORE the gap — this is the actual name.
        clean = re.split(r'\s{3,}|\t', line)[0].strip()
        # Strip credential/qualification suffixes enclosed in brackets:
        # "Rahul Shah [CA – India, CPA – USA (Awaiting License), LLB]" → "Rahul Shah"
        clean_no_creds = re.sub(r'\s*[\[\(][^\]\)]*[\]\)].*', '', clean).strip()
        # Also strip trailing credential abbreviations after comma/pipe:
        # "Rahul Shah, CA, CPA" → "Rahul Shah"
        clean_no_creds = re.sub(
            r'\s*[,|]\s*(CA|CPA|CFA|MBA|LLB|PhD|MD|BCA|MCA|BE|BTech|MTech|'
            r'MSc|BSc|BBA|MFin|MSF|CMA|FCA|FCCA|ACCA|ACA)[\s,|].*$',
            '', clean_no_creds, flags=re.IGNORECASE).strip()
        # Guard on the CLEAN portion (not the full line which may have trailing spaces+location)
        if len(clean_no_creds) > 60:
            continue
        if _NAME_JUNK_RE.match(clean_no_creds) or _LOCATION_LINE_RE.match(clean_no_creds):
            continue
        # Must look like a real name: 1-5 words, all starting with a letter,
        # no special chars like brackets or punctuation
        # Single-word names must be ≥3 chars (rejects "YN", "BA", etc.)
        words = clean_no_creds.split()
        if (1 <= len(words) <= 5
                and all(w[0].isalpha() for w in words)
                and not re.search(r'[(){}\[\]<>@#$%^&*]', clean_no_creds)
                and not (len(words) == 1 and len(clean_no_creds) < 3)):
            name = clean_no_creds
            break
    # If we still have "Candidate", fall back to the first ≤50-char non-junk line
    # that looks like a proper name (1-4 words, alpha-starting, no special chars).
    if name == "Candidate":
        for line in lines[:8]:
            # Also try bracket-stripping on fallback candidates
            line_stripped = re.sub(r'\s*[\[\(][^\]\)]*[\]\)].*', '', line).strip()
            line_stripped = re.sub(
                r'\s*[,|]\s*(CA|CPA|CFA|MBA|LLB|PhD|MD|BCA|MCA|BE|BTech|MTech|'
                r'MSc|BSc|BBA|MFin|MSF|CMA|FCA|FCCA|ACCA|ACA)[\s,|].*$',
                '', line_stripped, flags=re.IGNORECASE).strip()
            if len(line_stripped) > 50 or not line_stripped or _NAME_JUNK_RE.match(line_stripped):
                continue
            if _LOCATION_LINE_RE.match(line_stripped):
                continue
            words = line_stripped.split()
            if (1 <= len(words) <= 4
                    and all(w[0].isalpha() for w in words)
                    and not re.search(r'[(){}\[\]<>@#$%^&*:,.]', line_stripped)
                    and not (len(words) == 1 and len(line_stripped) < 3)):
                name = line_stripped
                break

    email    = next((EMAIL_RE.search(l).group() for l in lines if EMAIL_RE.search(l)), "")
    phone    = next((PHONE_RE.search(l).group().strip() for l in lines if PHONE_RE.search(l)), "")
    linkedin = next((LINKEDIN_RE.search(l).group() for l in lines if LINKEDIN_RE.search(l)), "")

    # Additional patterns that mark a line as a section heading (not a title)
    _SECTION_HEADING_RE = re.compile(
        r'^(personal\s+data|personal\s+information|contact|references?|'
        r'interests?|hobbies|activities|volunteer|awards?|certific\w*|'
        r'licens\w*|publications?|languages?|additional\s+information|'
        r'marital\s+status|birth\s+(date|place)|nationality|citizenship|'
        r'date\s+of\s+birth|gender|sex\b)',
        re.IGNORECASE
    )

    # Classify remaining lines as location or title
    location = ""
    title    = ""
    for line in lines[1:]:
        if EMAIL_RE.search(line) or PHONE_RE.search(line) or LINKEDIN_RE.search(line):
            continue
        ll = line.lower()
        is_location = (
            any(kw in ll for kw in LOCATION_KEYWORDS)
            and not _CREDENTIAL_LINE_RE.search(line)   # reject "CPA (USA) | CMA (USA"
        )
        if is_location and not location:
            location = line
        elif not title and not is_location and 3 < len(line) < 120:
            # Reject section headings, sentence fragments (ends with "."), name repetition
            if _NAME_JUNK_RE.match(line) or _SECTION_HEADING_RE.match(line):
                continue
            if line.rstrip().endswith('.') and len(line) > 5:
                continue  # sentence fragment (e.g. "organizations.", "Inc.")
            if line.strip() == name:
                continue  # title is just the name again (e.g. European CV format)
            # Reject lines starting with non-alphanumeric chars (icons, bullets, etc.)
            if line and not line[0].isalnum():
                continue
            # Reject short lines ending with digits — likely a street address or ZIP code
            # (e.g. "Xochicalco 295", "03020 Mexico City")
            if re.search(r'\d+\s*$', line) and len(line) < 40:
                continue
            if len(line) > 3:
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
        # Strip leading/trailing whitespace, then common bullet/dash chars
        # including ● (U+25CF) and ○ (U+25CB) which are not in the ASCII bullet set.
        s = s.strip().strip("–-•·▸►▪●○◆◇▶▷").strip().rstrip('.')
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
        # Reject language-name headers ("Spanish:", "English:") that end with ":"
        # These come from Skills sections that actually contain language info.
        if s.endswith(':'):
            continue
        # Reject metric/KPI statements masquerading as skills.
        # e.g. "Collected an average of 100 calls per agent per day"
        if re.search(r'\b(an average|per agent|per day|per week|per month|per year'
                     r'|\d+\s*%|\d+\s*calls|\d+\s*tickets|\d+\s*clients)\b', s, re.IGNORECASE):
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
    #   "Feb 2023 ~ Mar 2026"       (tilde separator, Indian-LinkedIn export)
    #   "09/2019 – actual"          (Spanish/French CVs: "actual" = current)
    #   "May/2023 – today"          (Brazilian format: month/year with slash)
    #   "Jun/2023-Aug/2024"         (Brazilian: month/year range, slash separator)
    r"(?:(?:\d{1,2}/)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?[\s/]+))?"
    r"\d{4}"
    r"\s*(?:[-–—~]+|to|till)\s*"
    r"(?:(?:\d{1,2}/)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?[\s/]+))?"
    r"(?:present|current|now|date|actual|today|hoje|\d{4})",
    re.IGNORECASE
)

# Supplementary: "MM/YYYY  Present" (no dash separator, just whitespace).
# Some CV formats (e.g. Giuliana) use two spaces between end-date and "Present".
_SPACE_DATE_RE = re.compile(
    r'(?:\d{1,2}[/-])?\d{4}\s+(?:present|current|now|actual|date|today|hoje)\b',
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

        # ── Column-order sanity check ────────────────────────────────────────
        # Some PDF rows are extracted in role→resp→org order instead of
        # org→role→resp (e.g. Mobilyte row in Vibhor's CV).
        # Detect: assigned "company" has role keywords but no company indicators,
        #         AND one of the bullet strings has company-name indicators.
        _CO_HINT = re.compile(
            r'\b(pvt|ltd|inc|llc|corp|gmbh|plc|solutions?|technologies?|'
            r'services?|systems?|industries|associates?|partners?|enterprises?|'
            r'software|digital|global|group)\b', re.IGNORECASE
        )
        _ROLE_KW = re.compile(
            r'\b(engineer|manager|lead|designer|developer|analyst|consultant|'
            r'director|architect|officer|executive|specialist|coordinator|'
            r'assistant|tester|programmer|scientist|supervisor)\b', re.IGNORECASE
        )
        if (org and role and bullets
                and _ROLE_KW.search(org)
                and not _CO_HINT.search(org)
                and _CO_HINT.search(bullets[-1])):
            # Last "bullet" is actually the company; role was assigned to org slot.
            # Order was: role → responsibilities → org
            actual_org  = bullets.pop()
            actual_role = org
            # The "role" slot held the first responsibility group
            extra_resp  = role
            org  = actual_org
            role = actual_role
            # Re-split extra_resp as additional bullets
            for part in re.split(r'[,;]', extra_resp):
                part = part.strip().lstrip("•·▸►▪-– ")
                if part and len(part) > 5:
                    bullets.insert(0, part)
            bullets = bullets[:6]

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

    # ── Pre-clean: strip duration suffixes appended to date lines ────────────
    # Some LinkedIn-exported PDFs append "‧3 years 5 months" after the date range.
    # Remove these so DATE_RANGE_RE can match the clean date.
    _DURATION_SUFFIX_RE = re.compile(r'[‧·]\s*\d+\s+years?\s+\d*\s*months?.*$', re.IGNORECASE)
    lines = [_DURATION_SUFFIX_RE.sub('', l) for l in lines]

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
        if not stripped:
            continue

        # ── Special case: bullet-prefixed date boundary (Andrea/European format) ──
        # Some CVs use "• 02/2023 – actual  Co-Founder at Fintesis" where the
        # bullet char opens the job entry line, not a responsibility bullet.
        # Detect: line starts with bullet AND contains a date match AND has
        # substantial title text after the date (≥ 10 chars).
        _stripped_no_bullet = stripped.lstrip("•·▸►▪ ")
        if stripped.startswith(BULLET_CHARS) and _stripped_no_bullet != stripped:
            _m_bullet_date = DATE_RANGE_RE.search(_stripped_no_bullet)
            if _m_bullet_date:
                # Rewrite the line without the bullet prefix so the rest of
                # the parser sees it as a plain date-boundary line.
                # Handles both:
                #   "• 02/2023 – actual  Co-Founder at Fintesis"  (date + role inline)
                #   "•  09/2019 – 03/2023"                        (date only → Format B forward scan)
                lines[i] = _stripped_no_bullet
                stripped  = _stripped_no_bullet
                is_boundary[i] = True
                continue

        if stripped.startswith(BULLET_CHARS):
            continue

        # (a) Direct match on this line (also check space-separated "03/2025  Present")
        m = DATE_RANGE_RE.search(stripped) or _SPACE_DATE_RE.search(stripped)
        _m_paren_enclosed = False   # True when DATE_RANGE_RE match is inside ()
        if m:
            # Skip dates that are entirely inside parentheses — these are sidebar
            # education location entries like "Santa Fe, Argentina (2010-2013)" that
            # pdfplumber interleaves into the experience text in multi-column PDFs.
            _ms, _me = m.start(), m.end()
            _before_paren = stripped[:_ms].rstrip()
            _after_paren  = stripped[_me:].lstrip()
            if _before_paren.endswith('(') and _after_paren.startswith(')'):
                # Date is inside parentheses — NOT a boundary by itself.
                # Do NOT `continue` here: fall through so the Format C (PAREN_YEAR_RE)
                # check below can still fire.  Example: a line ending with "(2022)"
                # that ALSO contains "(2018–2019)" in its body text should be caught
                # as a Format C boundary via the trailing "(2022)".
                _m_paren_enclosed = True
            else:
                is_boundary[i] = True
                continue

        # (b) Merge with next line — ONLY mark if match starts within stripped.
        # Skip when date is already identified as paren-enclosed: the merged
        # string would still find the same paren-enclosed date first, triggering
        # a false positive (e.g. "Santa Fe, Argentina (2010-2013)" + next line).
        if not _m_paren_enclosed and i + 1 < len(lines):
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
            _lower_stripped = stripped.lower()
            # Reject cert/education sidebar entries interleaved by pdfplumber:
            # "Talent Acquisition Certificate - Edutin Academy (2025)"
            _CERT_EDU_WORDS = {
                'certificate', 'certification', 'academy', 'training', 'course',
                'diploma', 'institute', 'college', 'school', 'university',
            }
            # Reject pure location-entry lines like "Santa Fe, Argentina (2010-2013)".
            # These are sidebar education locations that pdfplumber mixes into the
            # experience text.  Signature: ≤4 words before the paren and NO role
            # indicators (em-dash, slash) that would signal a job description.
            _before_pm = stripped[:pm.start()].strip().rstrip(',')
            _bw_count  = len(_before_pm.replace(',', ' ').split())
            _looks_like_location = (
                _bw_count < 4   # "Santa Fe, Argentina" = 3 words → location
                                 # "Office Manager, Visas USA" = 4 words → keep
                and '–' not in _before_pm
                and '/' not in _before_pm
                and not any(w in _before_pm.lower() for w in _CERT_EDU_WORDS)
            )
            if (not _looks_like_location
                    and not any(w in _lower_stripped for w in _CERT_EDU_WORDS)):
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
                dm = DATE_RANGE_RE.search(bl) or _SPACE_DATE_RE.search(bl)
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
    # Track globally which lines have been claimed by any boundary's header_indices.
    # This prevents a forward-scan claim from being re-used by the next boundary's backward scan.
    _all_claimed: set[int] = set()

    # Modifiers that signal a work location (remote / hybrid / on-site), not part of the role title.
    _LOCATION_MODIFIERS = {'remote', 'hybrid', 'onsite', 'on-site', 'in-office', 'presencial'}

    def _split_role_company(text: str):
        """
        Split a role+company string into (role, company).

        Handles two separator styles:
        1. Pipe: "Company | Location Role" or "Company | Location Role | extra"
           — used by Diego-style CVs where company precedes the role on the same line.
        2. Comma: "Role Title, Company Name"
           — classic format; only splits on the LAST comma.
        Returns (text, "") when no clean split is found.
        """
        if '|' in text:
            parts = [p.strip() for p in text.split('|')]
            if len(parts) >= 2:
                company = parts[0].strip()
                role = parts[-1].strip()
                # Strip leading work-location modifier from role
                role_words = role.split()
                if role_words and role_words[0].lower() in _LOCATION_MODIFIERS:
                    role = ' '.join(role_words[1:])
                if role and company:
                    return role, company
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

        # Pull period from the date match (also check space-sep "03/2025  Present")
        dm = DATE_RANGE_RE.search(date_str) or _SPACE_DATE_RE.search(date_str)
        period     = dm.group().strip() if dm else date_str
        _date_strip_re = DATE_RANGE_RE if DATE_RANGE_RE.search(date_str) else _SPACE_DATE_RE
        before_date = re.sub(_date_strip_re, "", date_str).strip().rstrip("–-|·,").strip()

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
                # ── Format B: look backward for role title above the date ──────
                #
                # First pass: standard backward scan (stop at any bullet/marker).
                # Handles: ROLE\nCOMPANY\nDATE (classic Format B).
                #
                # Second pass (fires only when first pass found nothing, meaning
                # the date appeared AFTER bullets): skip the entire bullet block
                # and take the line immediately above the bullets as the role.
                # Handles: COMPANY\nROLE\n• bullet\n• bullet\nDATE (date-at-end).
                #
                # "Ø" is used by some CVs as a company-section marker (like a bullet).
                # Treat it as a stop character in first-pass backward scans.
                _EXTRA_STOP = ("Ø",)

                # Sidebar section headings that pdfplumber interleaves into experience text.
                # When encountered going backward, stop — we've left the role/company block.
                _SIDEBAR_LABEL_RE = re.compile(
                    r'^(contact|phone|email|address|location|education|skills|tools'
                    r'|languages?|certifications?|awards?|achievements?|interests?'
                    r'|hobbies|references?|linkedin|github|portfolio|website|summary'
                    r'|profile|objective)\s*$',
                    re.IGNORECASE,
                )
                # Lines with parenthesised year ranges are sidebar education entries.
                # Skip (not stop) — the real role/company may be above them.
                _PAREN_YEAR_RE_SCAN = re.compile(r'\(\s*\d{4}[-–]?\d{0,4}\s*\)')

                title_candidates = []
                j = bi - 1
                while j > prev_bi and len(title_candidates) < 2:
                    s = lines[j].strip()
                    if s:
                        if s.startswith(BULLET_CHARS) or s.startswith(_EXTRA_STOP):
                            break
                        # Reject lines already claimed by a previous forward scan
                        if j in _all_claimed:
                            break
                        # Reject continuation sentences (end with "." / start lowercase)
                        if s.endswith('.') or (s and s[0].islower()):
                            break
                        # Stop at sidebar section headings (pdfplumber interleaving)
                        if _SIDEBAR_LABEL_RE.match(s):
                            break
                        # Skip sidebar education entries like "Santa Fe, Argentina (2010-2013)"
                        if _PAREN_YEAR_RE_SCAN.search(s):
                            j -= 1
                            continue
                        # Skip education institution name lines like "Eduardo Lafferriere Institute"
                        # that pdfplumber interleaves between real role/company lines.
                        # Detect: short line ending with an institution suffix word.
                        _EDU_INST_SUFFIX_RE = re.compile(
                            r'\b(institute|college|university|school|academy'
                            r'|polytechnic|institution|foundation)\s*$',
                            re.IGNORECASE,
                        )
                        if _EDU_INST_SUFFIX_RE.search(s) and len(s) < 70:
                            j -= 1
                            continue
                        # Skip degree-title lines like "BA in English Teaching",
                        # "MSc Computer Science", "Business Administration Diploma"
                        _DEGREE_PREFIX_RE = re.compile(
                            r'^(B\.?A\.?|B\.?S\.?|M\.?A\.?|M\.?S\.?|M\.?B\.?A\.?'
                            r'|Ph\.?D\.?|Lic\.|Ing\.|Dr\.|Esp\.|Post[- ]?grad\b)',
                            re.IGNORECASE,
                        )
                        _DEGREE_SUFFIX_RE = re.compile(
                            r'\b(diploma|certificate|degree)\s*$',
                            re.IGNORECASE,
                        )
                        if (_DEGREE_PREFIX_RE.match(s) or _DEGREE_SUFFIX_RE.search(s)) and len(s) < 70:
                            j -= 1
                            continue
                        title_candidates.insert(0, (j, s))
                    j -= 1

                _bullet_skipped = False
                if not title_candidates:
                    # Second pass: skip bullet block to find role above it
                    j = bi - 1
                    while j > prev_bi and not lines[j].strip():
                        j -= 1
                    # Skip consecutive bullet lines
                    skipped = 0
                    while j > prev_bi and lines[j].strip().startswith(BULLET_CHARS):
                        j -= 1
                        skipped += 1
                    if skipped > 0:  # bullets were present
                        while j > prev_bi and not lines[j].strip():
                            j -= 1
                        if j > prev_bi:
                            s = lines[j].strip()
                            if (s
                                    and not s.startswith(BULLET_CHARS)
                                    and not s.startswith(_EXTRA_STOP)
                                    and not s.endswith('.')
                                    and not (s and s[0].islower())
                                    and j not in _all_claimed
                                    and not (DATE_RANGE_RE.search(s) and len(s) < 40)):
                                title_candidates = [(j, s)]
                                _bullet_skipped = True

                if len(title_candidates) >= 2:
                    role    = title_candidates[-2][1].rstrip("–-|·,").strip()
                    company = title_candidates[-1][1].strip()
                    new_idx = {title_candidates[-2][0], title_candidates[-1][0]}
                    header_indices.update(new_idx)
                    _all_claimed.update(new_idx)
                elif title_candidates:
                    role    = title_candidates[-1][1].rstrip("–-|·,").strip()
                    company = ""
                    header_indices.add(title_candidates[-1][0])
                    _all_claimed.add(title_candidates[-1][0])
                else:
                    # ── Format B-forward: role (and company) appear after the date ──
                    # Handles two sub-patterns:
                    #   • "• DATE\nROLE TITLE" (Andrea / European CVs)
                    #   • "DATE\nCOMPANY\nROLE" (Saundarya — date-before-company)
                    # Collect up to 2 non-bullet, non-date, title-case lines.
                    fwd_candidates = []
                    for fwd in range(bi + 1, min(bi + 6, _next_search_end)):
                        s = lines[fwd].strip()
                        if not s:
                            continue
                        if s.startswith(BULLET_CHARS) or s.startswith(_EXTRA_STOP):
                            break
                        if DATE_RANGE_RE.search(s) or _SPACE_DATE_RE.search(s):
                            break
                        # Skip sidebar labels that pdfplumber interleaves
                        if _SIDEBAR_LABEL_RE.match(s):
                            continue
                        # Skip parenthesised year sidebar edu entries
                        if _PAREN_YEAR_RE_SCAN.search(s):
                            continue
                        # Reject sentence fragments
                        if s.endswith('.') or (s and s[0].islower()):
                            continue
                        if len(s) < 80 and not any(kw in s.lower() for kw in LOCATION_KEYWORDS):
                            fwd_candidates.append((fwd, s))
                            _all_claimed.add(fwd)
                            if len(fwd_candidates) >= 2:
                                break

                    if len(fwd_candidates) >= 2:
                        first_text  = fwd_candidates[0][1]
                        second_text = fwd_candidates[1][1]
                        # Heuristic: if the first forward line is ALL-CAPS it's a company
                        # name (brand), and the second is the role title. Otherwise the
                        # first is the role.  E.g. "ULTRAHUMAN\nAssociate Manager".
                        if first_text.isupper():
                            role    = second_text.rstrip("–-|·, ")
                            company = first_text
                        else:
                            role    = first_text.rstrip("–-|·, ")
                            company = second_text
                        header_indices.update(fc[0] for fc in fwd_candidates)
                    elif fwd_candidates:
                        role    = fwd_candidates[0][1].rstrip("–-|·, ")
                        company = ""
                        header_indices.add(fwd_candidates[0][0])
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
                # Skip wrapped location fragments, e.g. "from Hyderabad)."
                # These are the tail of a long role-header line that wrapped.
                if (re.match(r'^(from|based\s+in|remote\s+from|located\s+in|working\s+from)\b',
                             bullet, re.IGNORECASE)
                        and ')' in bullet and len(bullet) < 60):
                    continue
                # Skip company-name suffix fragments (tail of a wrapped role-header
                # line), e.g. "Limited, Dublin" wrapping from "MyComplianceOffice\n
                # Limited, Dublin" where the backward-scan only picked up the first line.
                if (re.match(
                        r'^(Ltd\.?|Limited|Inc\.?|Corp\.?|GmbH|S\.?A\.?|PLC|LLC|'
                        r'B\.?V\.?|Ireland\s+Ltd\.?|Ireland\s+Limited)\b',
                        bullet, re.IGNORECASE)
                        and len(bullet) < 60):
                    continue

                # Continuation detection — two cases where a non-bulleted line is
                # a wrapped continuation of the previous bullet, not a new bullet:
                #
                # Case A: starts with lowercase → always a wrap (e.g. "engineering team.")
                # Case B: previous bullet ended WITHOUT sentence-closing punctuation
                #         AND this line is SHORT (< 30 chars) — a wrapped noun fragment
                #         (e.g. "…to deliver an Enterprise\nGrade Product." → 13 chars)
                #         Long lines (≥ 30 chars) without prefix are likely new thoughts.
                _is_wrap = (
                    bullets
                    and not s.startswith(tuple(BULLET_CHARS))
                    and bullet
                    and (
                        bullet[0].islower()                              # Case A
                        or (bullets[-1][-1] not in '.!?:'               # Case B
                            and len(bullet) < 30)
                    )
                )
                if _is_wrap:
                    bullets[-1] = bullets[-1].rstrip() + " " + bullet
                else:
                    bullets.append(bullet)

        # ── Merge dangling continuation bullets ───────────────────────────────
        # Lines like "Leading the teams and" end mid-sentence; the next line is
        # the logical continuation.  Merge them so bullets stay coherent.
        _DANGLING_END_RE = re.compile(
            r'\b(and|or|to|of|the|with|for|in|a|an|that|which|by|from|on|at|as|'
            r'is|are|be|been|being|its|their|our|this|these|those|both|either|'
            r'neither|within|across|between|through|into|including|excluding|'
            r'such|where|when|while|how|why|what|any|all|each|every|some|no|'
            r'not|only|also|but|yet|so|then|than|more|most|less|least|very|'
            r'too|quite|rather|further|upon|per|over|under)\s*$',
            re.IGNORECASE
        )
        merged_bullets: list[str] = []
        bi2 = 0
        while bi2 < len(bullets):
            b = bullets[bi2]
            while bi2 + 1 < len(bullets) and _DANGLING_END_RE.search(b):
                bi2 += 1
                b = b.rstrip() + " " + bullets[bi2].lstrip()
            merged_bullets.append(b.strip())
            bi2 += 1
        bullets = merged_bullets

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
    summary = re.sub(r'\n+', ' ', summary)              # collapse inline newlines
    summary = re.sub(r'  +', ' ', summary).strip()     # normalise multiple spaces

    # ── Summary length guard ─────────────────────────────────────────────────
    # Candidates sometimes paste their entire LinkedIn "About" (10+ bullets,
    # 300+ words) into the CV.  A wall of text in the Professional Summary box
    # is worse than no summary at all — it buries the candidate's experience.
    # Rule: if the summary exceeds 80 words, remove it entirely.
    if len(summary.split()) > 80:
        summary = ""

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
    # Known city/country names that should NEVER appear as skills even when
    # they happen to be inside parentheses in an experience bullet.
    _LOCATION_NAMES = {
        'hyderabad','bangalore','mumbai','delhi','chennai','pune','kolkata',
        'dublin','london','new york','paris','amsterdam','berlin','madrid',
        'sydney','toronto','singapore','dubai','hong kong','rio de janeiro',
        'são paulo','bogotá','lima','santiago','buenos aires','mexico city',
        'ireland','india','usa','uk','brazil','brasil','australia','canada',
        'remote','hybrid','onsite','on-site','on site',
    }
    if not skills and experience:
        inferred = []
        seen = set()
        for exp_item in experience[:4]:
            for bullet in exp_item.get("bullets", [])[:4]:
                # Extract parenthesised tool lists: "CRM platforms (HubSpot, GoHighLevel)"
                for group in re.findall(r'\(([^)]{5,60})\)', bullet):
                    for item in re.split(r'[,;]', group):
                        item = item.strip()
                        if (3 < len(item) < 28
                                and item.lower() not in seen
                                and item.lower() not in _LOCATION_NAMES):
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

            # Check for "ORG as ROLE" boundary (e.g. "Cyient Ltd as Engineer")
            m_ar = re.match(r'^(.+?)\s+as\s+(.+)$', s, re.IGNORECASE)
            if m_ar and not s.startswith(tuple(BULLET_CHARS_SET)):
                potential_org  = m_ar.group(1).strip()
                potential_role = m_ar.group(2).strip()
                # Sanity: both sides must look like a real "Company as Job Title" pair.
                # Rejects narrative uses of "as": "well as US & Canada" (org=sentence fragment),
                # "Poland, Germany as well as US" (org=list of places), etc.
                _role_lower = potential_role.strip().lower()
                _org_lower  = potential_org.strip().lower()
                if (len(potential_org) < 80 and len(potential_role) < 60
                        and len(potential_org) > 2   # not a 1-2 char pronoun ("I", "He")
                        and potential_org[0].isupper()
                        # Reject "X as well as Y" — role starts with "well"/"also"/"too"
                        and not _role_lower.startswith(('well ', 'also ', 'too ', 'such '))
                        # Reject org with multiple commas (list of countries/places)
                        and potential_org.count(',') < 2
                        # Reject org that contains conjunctions or prepositions
                        and not re.search(r'\b(and|or|but|for|from|with|within)\b', _org_lower)):
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
            # When overflow rescue fires on a table-format CV, the "projects" section
            # typically also contains table artifacts: role names and company names
            # that were mis-routed there.  Detect by checking if ALL projects have
            # no bullets AND no description — pure title strings = garbage.
            if projects and all(
                not p.get('bullets') and not p.get('description', '').strip()
                for p in projects
            ):
                projects = []

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

    # ── Location fallback: scan full text when header gave nothing ────────────
    # Tries to find a clean city/country line from the education or experience
    # sections (e.g. "Mumbai, India" inside a university line, or "London, UK"
    # in a company address).  Prefers short lines (≤ 60 chars) that contain a
    # LOCATION_KEYWORD and are not credential chains.
    if not header["candidate_location"]:
        _LOC_CANDIDATE_RE = re.compile(
            r'^[A-Za-zÀ-ÖØ-öø-ÿ\s,./\-()]+$'  # only letters, spaces, punctuation
        )
        for line in text.splitlines():
            s = line.strip()
            if not s or len(s) > 80 or len(s) < 3:
                continue
            sl = s.lower()
            if not any(kw in sl for kw in LOCATION_KEYWORDS):
                continue
            if _CREDENTIAL_LINE_RE.search(s):
                continue
            if EMAIL_RE.search(s) or PHONE_RE.search(s) or LINKEDIN_RE.search(s):
                continue
            # Reject lines with digits (dates, zip codes, street numbers)
            if re.search(r'\d', s):
                continue
            # Reject long sentences (part of a narrative)
            if len(s.split()) > 10:
                continue
            # Prefer lines that look like "City, Country" or just "Country"
            if _LOC_CANDIDATE_RE.match(s):
                header["candidate_location"] = s
                break

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

    # Calculate total years of professional experience from role periods
    yrs = 0
    for exp in data.get("experience", []):
        period = exp.get("period", "")
        yr_matches = re.findall(r"\d{4}", period)
        if not yr_matches:
            continue
        start = int(yr_matches[0])
        if any(k in period.lower() for k in ("present", "current", "now", "today")):
            end = datetime.now().year
        elif len(yr_matches) >= 2:
            end = int(yr_matches[1])
        else:
            continue  # single year without "present" — can't determine duration
        yrs += max(0, end - start)
    entry["years_experience"] = yrs

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
    safe_name = re.sub(r"[^\w\s-]", "", data["candidate_name"]).strip()
    # Guard: macOS limits filenames to 255 bytes. Multi-column PDFs sometimes
    # collapse all text into one line, making candidate_name 10 000+ chars.
    # Truncate to 80 chars; if still blank after cleanup, use "Unknown Candidate".
    safe_name = safe_name[:80].strip()
    if not safe_name or safe_name.lower() in ("candidate", "curriculum vitae", "resume", "cv"):
        safe_name = "Unknown Candidate"
    pdf_filename = f"{safe_name} - CV Simpalm Staffing.pdf"
    output_path  = Path(__file__).parent.parent / "processed" / pdf_filename

    # 4. Render PDF
    render_pdf(data, str(output_path))

    # 5. Update registry
    update_index(data, pdf_filename, source_path=cv_path)

    print(f"\n🎉 Done! → processed/{pdf_filename}")

if __name__ == "__main__":
    main()
