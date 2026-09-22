"""
Job Monitor - checks career pages for frontend/React/MERN openings
and emails you when something new shows up.

Uses a real headless browser (Playwright/Chromium) so JavaScript-rendered
career pages (React, Next.js, Angular SPAs — the majority of modern sites)
are actually loaded, not just their empty raw HTML.

Free automation pattern: this script is meant to be run on a schedule
by GitHub Actions (free), not kept running on your own machine.
See README.md for setup.
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
STATE_FILE = BASE_DIR / "seen_jobs.json"

# Keywords that mean "this looks like a job I want"
KEYWORDS = [
    r"frontend\s*developer",
    r"front[\s-]?end\s*developer",
    r"react\s*(js)?\s*developer",
    r"react\.js",
    r"next\.js",
    r"mern\s*stack",
    r"ui\s*developer",
    r"javascript\s*developer",
    r"web\s*developer",
]
KEYWORD_RE = re.compile("|".join(KEYWORDS), re.IGNORECASE)

# Words that usually mean "not for me" — skip these even if a keyword matched
EXCLUDE_RE = re.compile(
    r"senior|sr\.|lead|principal|10\+\s*years|8\+\s*years|7\+\s*years|6\+\s*years|5\+\s*years",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch_rendered_html(browser, url: str, wait_ms: int = 4000) -> str:
    """Load a page in a real headless browser and return the HTML
    *after* JavaScript has run, so client-side-rendered job listings
    (React/Next.js/Angular sites) actually show up."""
    page = browser.new_page(user_agent=USER_AGENT)
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        # give JS-rendered lists a moment to populate
        page.wait_for_timeout(wait_ms)
        html = page.content()
    finally:
        page.close()
    return html


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def extract_candidate_lines(html: str):
    """Pull short, heading-like lines of text out of a page — these are
    where job titles usually live (h1-h4, links, list items)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    lines = set()
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "a", "li", "span", "p"]):
        text = tag.get_text(strip=True)
        if text and 3 <= len(text) <= 120:
            lines.add(text)
    return lines


def find_matching_jobs(lines):
    matches = []
    for line in lines:
        if KEYWORD_RE.search(line) and not EXCLUDE_RE.search(line):
            matches.append(line)
    return sorted(set(matches))


def send_email(new_findings: dict):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    to_email = os.environ.get("TO_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        print("SMTP_USER / SMTP_PASS not set — skipping email, printing instead.")
        print(json.dumps(new_findings, indent=2, ensure_ascii=False))
        return

    body_lines = ["New matching postings found:\n"]
    for company, jobs in new_findings.items():
        body_lines.append(f"\n{company}:")
        for job in jobs:
            body_lines.append(f"  - {job}")
    body = "\n".join(body_lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Job alert: {sum(len(v) for v in new_findings.values())} new posting(s)"
    msg["From"] = smtp_user
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
    print(f"Email sent to {to_email}")


def main():
    companies = load_json(COMPANIES_FILE, [])
    seen = load_json(STATE_FILE, {})  # {company_name: [job strings already notified]}

    new_findings = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for company in companies:
                name, url = company["name"], company["url"]
                try:
                    html = fetch_rendered_html(browser, url)
                except Exception as e:
                    print(f"[skip] {name}: could not load ({e})")
                    continue

                lines = extract_candidate_lines(html)
                matches = find_matching_jobs(lines)

                already_seen = set(seen.get(name, []))
                fresh = [m for m in matches if m not in already_seen]

                if fresh:
                    new_findings[name] = fresh
                    print(f"[NEW] {name}: {fresh}")
                else:
                    print(f"[ok] {name}: no new matches ({len(matches)} known)")

                # update state with everything currently seen for this company
                seen[name] = matches
        finally:
            browser.close()

    STATE_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")

    if new_findings:
        send_email(new_findings)
    else:
        print("No new matches this run.")


if __name__ == "__main__":
    sys.exit(main())
