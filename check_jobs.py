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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
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


def build_html_body(new_findings: dict, company_urls: dict) -> str:
    """new_findings: {company_name: [job strings]}
    company_urls:  {company_name: career_page_url}
    Produces a clean HTML email with each company name linked to its
    career page and jobs listed as bullets underneath.
    """
    total = sum(len(v) for v in new_findings.values())

    rows = []
    for company, jobs in new_findings.items():
        url = company_urls.get(company, "#")
        job_items = "".join(f"<li style='margin:4px 0;'>{escape(job)}</li>" for job in jobs)
        rows.append(f"""
        <div style="margin-bottom:20px;">
          <a href="{escape(url)}" style="font-size:16px;font-weight:bold;color:#1a73e8;text-decoration:none;">
            {escape(company)} &rarr;
          </a>
          <ul style="margin:6px 0 0 0;padding-left:20px;">
            {job_items}
          </ul>
        </div>
        """)

    html = f"""\
    <html>
      <body style="font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.5;">
        <h2 style="margin-bottom:4px;">🎯 {total} new job posting(s) found</h2>
        <p style="color:#666;margin-top:0;">Click a company name to open its careers page.</p>
        {''.join(rows)}
        <hr style="border:none;border-top:1px solid #eee;margin-top:24px;">
        <p style="color:#999;font-size:12px;">Sent automatically by your job monitor script.</p>
      </body>
    </html>
    """
    return html


def build_plaintext_body(new_findings: dict, company_urls: dict) -> str:
    """Plain-text fallback for email clients that don't render HTML."""
    lines = ["New matching postings found:\n"]
    for company, jobs in new_findings.items():
        url = company_urls.get(company, "")
        lines.append(f"\n{company} ({url}):" if url else f"\n{company}:")
        for job in jobs:
            lines.append(f"  - {job}")
    return "\n".join(lines)


def send_email(new_findings: dict, company_urls: dict):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    to_email = os.environ.get("TO_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        print("SMTP_USER / SMTP_PASS not set — skipping email, printing instead.")
        print(json.dumps(new_findings, indent=2, ensure_ascii=False))
        return

    total = sum(len(v) for v in new_findings.values())

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Job alert: {total} new posting(s)"
    msg["From"] = smtp_user
    msg["To"] = to_email

    # Attach plain text first, HTML second — email clients prefer the last
    # part that they can render, so HTML is used when supported.
    msg.attach(MIMEText(build_plaintext_body(new_findings, company_urls), "plain", "utf-8"))
    msg.attach(MIMEText(build_html_body(new_findings, company_urls), "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
    print(f"Email sent to {to_email}")


def main():
    companies = load_json(COMPANIES_FILE, [])
    seen = load_json(STATE_FILE, {})  # {company_name: [job strings already notified]}

    new_findings = {}
    company_urls = {c["name"]: c["url"] for c in companies}

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
        send_email(new_findings, company_urls)
    else:
        print("No new matches this run.")


if __name__ == "__main__":
    sys.exit(main())
