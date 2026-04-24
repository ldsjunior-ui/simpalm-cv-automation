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
            if re.match(pattern, stripped):
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

def parse_skills(skills_text: str) -> list:
    if not skills_text:
        return []
    # Split by common delimiters: comma, pipe, bullet, newline, semicolon
    raw = re.split(r"[,|•·;\n]+", skills_text)
    skills = []
    for s in raw:
        s = s.strip().strip("–-•·▸►▪")
        if s and 2 < len(s) < 50:
            skills.append(s)
    return skills  # no cap

# ── Language Parsing ──────────────────────────────────────────────────────────

LEVEL_MAP = {
    "native":      100, "bilingual":   100,
    "fluent":       90, "advanced":     80,
    "upper":        75, "intermediate": 60,
    "basic":        35, "beginner":     25,
    "elementary":   30, "professional": 85,
}

def parse_languages(lang_text: str) -> list:
    if not lang_text:
        return []
    languages = []
    # Split by lines first, then also by bullet/separator chars within each line
    raw_lines = lang_text.splitlines()
    entries = []
    for line in raw_lines:
        # If a single line contains multiple languages separated by bullets/pipes
        parts = re.split(r"[•·|/\\]", line)
        entries.extend(parts)

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
        # Language name: first word(s) before any level keyword or punctuation
        lang_name = re.split(r"[-–:|,\(]", entry)[0].strip()
        lang_name = re.sub(r"\b(" + "|".join(LEVEL_MAP.keys()) + r")\b", "", lang_name, flags=re.IGNORECASE).strip()
        lang_name = re.sub(r"\s+", " ", lang_name).strip()
        if lang_name and 1 < len(lang_name) < 40:
            languages.append({"name": lang_name, "level": level, "percent": percent})
    return languages  # no cap

# ── Education Parsing ─────────────────────────────────────────────────────────

DEGREE_RE = re.compile(
    r"(?i)(b\.?sc?\.?|m\.?sc?\.?|ph\.?d\.?|mba|bachelor|master|post.?grad|graduate|associate|diploma|licenc)",
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
        for line in lines:
            if DEGREE_RE.search(line) and not degree:
                degree = line
            elif degree and not institution:
                institution = line.lstrip("•·▸►▪-– ").strip()
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
    r"(?:(?:\d{1,2}/)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?\s+))?"
    r"\d{4}"
    r"\s*[-–—to]+\s*"
    r"(?:(?:\d{1,2}/)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?\s+))?"
    r"(?:present|current|now|\d{4})",
    re.IGNORECASE
)

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
    """
    if not exp_text:
        return []

    BULLET_CHARS = ("•", "·", "▸", "►", "▪", "-", "–")
    # Supplementary: lone year in parens, e.g.  "(2022)" or "(2018–2019)"
    PAREN_YEAR_RE = re.compile(r'\(\s*(\d{4})\s*(?:[–\-]\s*(\d{4}|\w+))?\s*\)\s*$')

    lines = [l.rstrip() for l in exp_text.splitlines()]
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

        # Treat pure date-artifact fragments (e.g. "/ 2011", "()", "(2018)") as empty
        if before_date and re.match(r'^[/\d\s–\-—()]+$', before_date):
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
            # ── Format B: date is alone; role title is 2 lines above ─────────
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

    for idx, rec in enumerate(role_records):
        bi      = rec["bi"]
        next_bi = boundary_indices[idx + 1] if idx + 1 < len(boundary_indices) else len(lines)
        next_header = role_records[idx + 1]["header_indices"] if idx + 1 < len(role_records) else set()

        bullets = []
        for k in range(bi + 1, next_bi):
            if k in next_header:
                continue
            s = lines[k].strip()
            if not s:
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

def parse_projects(text: str) -> list:
    if not text:
        return []
    items = []
    blocks = re.split(r"\n{2,}", text)
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if lines:
            items.append({"title": lines[0], "description": " ".join(lines[1:])[:300]})
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

# ── Full Parse ────────────────────────────────────────────────────────────────

def parse_cv(text: str) -> dict:
    sections        = split_sections(text)
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
    stats           = generate_stats(experience, languages)
    summary         = sections.get("summary", "").strip()

    # Derive title from first experience role if header had none
    if not header["candidate_title"] and experience:
        raw = experience[0]["role"]
        derived = re.sub(DATE_RANGE_RE, "", raw).strip().strip("–-|·").strip()
        # Shorten long role titles to a clean summary
        parts = re.split(r"[|,&]", derived)
        header["candidate_title"] = " · ".join(p.strip() for p in parts[:3] if p.strip())[:100]

    # Auto-generate summary if the CV has no summary section but has experience
    if not summary and experience:
        # Use Title Case for name and title so they read naturally in a sentence
        raw_name  = header.get("candidate_name", "") or ""
        raw_title = header.get("candidate_title", "") or experience[0].get("role", "")
        name       = raw_name.title() if raw_name else "This candidate"
        title_tc   = raw_title.title() if raw_title else ""
        n_roles    = len(experience)

        # Collect unique companies (up to 3)
        companies  = []
        for exp in experience:
            c = exp.get("company", "").strip()
            if c and c not in companies:
                companies.append(c)
        companies = companies[:3]

        # Choose correct article (a / an) based on first sound of the title
        vowels       = "AEIOUaeiou"
        article      = "an" if title_tc and title_tc[0] in vowels else "a"
        title_phrase = f"{article} {title_tc}" if title_tc else "an accomplished professional"
        roles_phrase = f"{n_roles} roles" if n_roles > 1 else "a key role"
        company_phrase = (
            f" at organisations including {', '.join(companies[:-1])} and {companies[-1]}"
            if len(companies) > 1
            else (f" at {companies[0]}" if companies else "")
        )
        summary = (
            f"{name} is {title_phrase} with a proven track record across {roles_phrase}"
            f"{company_phrase}. "
            f"Their background reflects strong operational capability, attention to detail, "
            f"and a commitment to excellence in every position held."
        )

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
