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

def extract_text_pdf(path: str) -> str:
    from pdfminer.high_level import extract_text
    return extract_text(path)

def extract_text_docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)

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
    "summary":    r"(?i)^(professional\s+)?summary|profile|about\s+me|objective",
    "experience": r"(?i)^(professional\s+|work\s+)?experience|employment|career\s+history",
    "education":  r"(?i)^education|academic|qualification",
    "skills":     r"(?i)^(core\s+|key\s+|technical\s+)?skills?|competenc|expertise",
    "languages":  r"(?i)^languages?",
}

def split_sections(text: str) -> dict:
    lines = text.splitlines()
    sections = {"_header": [], "summary": [], "experience": [], "education": [], "skills": [], "languages": []}
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
    return skills[:15]  # cap at 15

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
    return languages[:5]

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
                institution = line
        if degree:
            education.append({"degree": degree, "institution": institution})
    return education[:4]

# ── Experience Parsing ────────────────────────────────────────────────────────

DATE_RANGE_RE = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?\s*)?\d{4}\s*[-–—to]+\s*((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\w]*\.?\s*)?\d{4}|"
    r"\d{4}\s*[-–—to]+\s*(present|current|now|\d{4})",
    re.IGNORECASE
)

def parse_experience(exp_text: str) -> list:
    if not exp_text:
        return []

    experience = []
    blocks = re.split(r"\n{2,}", exp_text)

    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue

        role = ""
        company = ""
        period = ""
        bullets = []

        for i, line in enumerate(lines):
            date_match = DATE_RANGE_RE.search(line)
            if date_match:
                period = date_match.group().strip()
                # Role is often the line before the date, or same line
                candidate_role = re.sub(DATE_RANGE_RE, "", line).strip().strip("–-|·")
                if candidate_role and not role:
                    role = candidate_role
                elif not role and i > 0:
                    role = lines[i - 1]
            elif i == 0 and not role:
                role = line
            elif i == 1 and not company and not date_match:
                company = line
            elif line.startswith(("•", "·", "▸", "►", "▪", "-", "–")) or (len(line) > 20 and i > 1):
                bullet = line.lstrip("•·▸►▪-– ").strip()
                if bullet and len(bullet) > 10:
                    bullets.append(bullet)

        if role:
            experience.append({
                "role":    role[:80],
                "company": company[:80],
                "period":  period or "",
                "bullets": bullets[:4],
            })

    return experience[:8]

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
    sections   = split_sections(text)
    header     = parse_header(sections.get("_header", ""))
    skills     = parse_skills(sections.get("skills", ""))
    languages  = parse_languages(sections.get("languages", ""))
    education  = parse_education(sections.get("education", ""))
    experience = parse_experience(sections.get("experience", ""))
    stats      = generate_stats(experience, languages)
    summary    = sections.get("summary", "").strip()

    # Derive title from first experience role if header had none
    if not header["candidate_title"] and experience:
        raw = experience[0]["role"]
        derived = re.sub(DATE_RANGE_RE, "", raw).strip().strip("–-|·").strip()
        # Shorten long role titles to a clean summary
        parts = re.split(r"[|,&]", derived)
        header["candidate_title"] = " · ".join(p.strip() for p in parts[:3] if p.strip())[:100]

    return {
        **header,
        "summary":    summary,
        "skills":     skills,
        "languages":  languages,
        "education":  education,
        "experience": experience,
        "stats":      stats,
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

def update_index(data: dict, pdf_filename: str):
    index = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else []

    # Remove existing entry for same candidate
    index = [e for e in index if e.get("name") != data["candidate_name"]]

    index.append({
        "name":      data["candidate_name"],
        "title":     data["candidate_title"],
        "location":  data["candidate_location"],
        "filename":  pdf_filename,
        "processed": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

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
    print(f"   Candidate: {data['candidate_name']}")
    print(f"   Title:     {data['candidate_title']}")
    print(f"   Skills:    {len(data['skills'])}")
    print(f"   Exp roles: {len(data['experience'])}")

    # 3. Build output filename
    safe_name   = re.sub(r"[^\w\s-]", "", data["candidate_name"]).strip()
    pdf_filename = f"{safe_name} - CV Simpalm Staffing.pdf"
    output_path  = Path(__file__).parent.parent / "processed" / pdf_filename

    # 4. Render PDF
    render_pdf(data, str(output_path))

    # 5. Update registry
    update_index(data, pdf_filename)

    print(f"\n🎉 Done! → processed/{pdf_filename}")

if __name__ == "__main__":
    main()
