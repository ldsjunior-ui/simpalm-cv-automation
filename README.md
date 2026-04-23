# Simpalm Staffing — CV Automation

Drop a CV into `/inbox/` → GitHub Actions processes it → branded PDF lands in `/processed/` → PalmDeck picks it up automatically.

## How it works

1. **Drop file** — push any `.pdf` or `.docx` CV to `inbox/`
2. **Auto-trigger** — GitHub Actions runs on every push to `inbox/**`
3. **Parse** — `pdfminer.six` / `python-docx` extract text; smart regex + section detection structure the data (100% free, zero API keys)
4. **Render** — Jinja2 fills the Simpalm template; WeasyPrint generates a print-quality PDF
5. **Publish** — PDF saved to `processed/` as `[Name] - CV Simpalm Staffing.pdf`; `index.json` updated
6. **PalmDeck** — fetches `index.json` via raw.githubusercontent.com, shows CV picker per candidate, PDF modal viewer inline

## Stack

| Step | Tool | License |
|------|------|---------|
| PDF extraction | pdfminer.six | MIT |
| DOCX extraction | python-docx | MIT |
| HTML templating | Jinja2 | BSD |
| PDF rendering | WeasyPrint | BSD |
| CI/CD | GitHub Actions | Free tier |

**Zero API keys. Zero cost. Fully open source.**

## File naming

Output: `[Candidate Name] - CV Simpalm Staffing.pdf`

## index.json structure

```json
[
  {
    "name": "Isabel Blanco Perez",
    "title": "Executive Assistant · Office Manager",
    "location": "Brazil — International",
    "filename": "Isabel Blanco Perez - CV Simpalm Staffing.pdf",
    "processed": "2026-04-23T10:00:00Z"
  }
]
```
